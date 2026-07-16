// Command oidc-server is a deliberately trivial OIDC provider for testing
// Teleport's generic_oidc join method. It is NOT suitable for any real use:
// it mints arbitrary, unauthenticated JWTs to anyone who asks.
//
// It serves, over HTTPS with a self-signed cert:
//
//	GET /.well-known/openid-configuration   OIDC discovery document
//	GET /keys                               JWKS (signing public key)
//	GET /token                              mint and return a signed JWT (text/plain)
//	GET /ca                                 the self-signed TLS CA cert (PEM, text/plain)
//	GET /healthz                            liveness probe
//
// Keys and the TLS cert are persisted under -data-dir so the JWKS and CA are
// stable across restarts (important: the token resource embeds them).
//
// Hostile modes (opt-in, default off) let a dedicated instance MISBEHAVE so we can
// exercise a client's defenses. See -oversize-endpoints / -oversize-bytes /
// -hang-after-oversize: they bloat the discovery and/or JWKS responses past a
// client's max-response-size cap, optionally holding the connection open forever
// after the oversized body — the case that would wedge a client that tries to drain
// an over-limit body instead of bailing out immediately.
package main

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var (
	issuer      = flag.String("issuer", "https://localhost:8443", "issuer URL; must match the JWT `iss`, discovery `issuer`, and the TLS cert host")
	addr        = flag.String("addr", ":8443", "listen address")
	dataDir     = flag.String("data-dir", "/data", "directory to persist the signing key and TLS cert")
	defAudience = flag.String("audience", "teleport.ethernet.fyi/generic-oidc-test", "default `aud` to embed when minting generic_oidc tokens")
	clusterName = flag.String("cluster-name", "teleport.ethernet.fyi", "Teleport cluster name; default `aud` for kubernetes-style tokens (k8s join requires the cluster name as audience)")
	ttl         = flag.Duration("ttl", 10*time.Minute, "lifetime of minted tokens (k8s join requires <= 30m)")
	extraSANs   = flag.String("extra-sans", "", "comma-separated extra DNS/IP SANs for the TLS cert")
	tlsCertFile = flag.String("tls-cert", "", "serve this TLS cert instead of a generated self-signed one (e.g. a wildcard LE cert); pair with -tls-key")
	tlsKeyFile  = flag.String("tls-key", "", "private key for -tls-cert")

	// Hostile knobs (default off): make a dedicated instance misbehave to test a
	// client's max-response-size handling. See the package doc.
	oversizeEndpoints = flag.String("oversize-endpoints", "", "comma-separated endpoints whose response bodies are padded past a client's size cap: 'discovery', 'jwks'")
	oversizeBytes     = flag.Int("oversize-bytes", 2*1024*1024, "bytes of filler to pad an oversized response with (default 2 MiB, past Teleport's 1 MiB cap)")
	hangAfterOversize = flag.Bool("hang-after-oversize", false, "after writing an oversized body, flush and block until the client disconnects (simulates a server that never closes the stream)")
)

const kid = "teleport-generic-oidc-test"

func main() {
	flag.Parse()
	if err := os.MkdirAll(*dataDir, 0o700); err != nil {
		log.Fatalf("creating data dir: %v", err)
	}

	signingKey, err := loadOrCreateRSAKey(filepath.Join(*dataDir, "signing-key.pem"))
	if err != nil {
		log.Fatalf("signing key: %v", err)
	}

	tlsCert, caPEM, err := loadOrCreateTLSCert()
	if err != nil {
		log.Fatalf("tls cert: %v", err)
	}

	oversize := map[string]bool{}
	for _, e := range strings.Split(*oversizeEndpoints, ",") {
		if e = strings.TrimSpace(e); e != "" {
			oversize[e] = true
		}
	}

	s := &server{signingKey: signingKey, caPEM: caPEM, oversize: oversize}

	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/openid-configuration", s.handleDiscovery)
	mux.HandleFunc("/keys", s.handleJWKS)
	mux.HandleFunc("/token", s.handleToken)
	mux.HandleFunc("/k8s/token", s.handleK8sToken)
	mux.HandleFunc("/ca", s.handleCA)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprintln(w, "ok") })

	srv := &http.Server{
		Addr:      *addr,
		Handler:   logRequests(mux),
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{tlsCert}},
	}

	log.Printf("trivial OIDC server starting")
	log.Printf("  issuer:   %s", *issuer)
	log.Printf("  listen:   %s", *addr)
	log.Printf("  audience: %s", *defAudience)
	log.Printf("  discovery: %s/.well-known/openid-configuration", *issuer)
	log.Printf("  mint:      %s/token?sub=test-bot", *issuer)
	log.Printf("  k8s mint:  %s/k8s/token  (aud=%s)", *issuer, *clusterName)
	if len(oversize) > 0 {
		log.Printf("  HOSTILE:  oversizing %v by %d bytes (hang-after-oversize=%t)", *oversizeEndpoints, *oversizeBytes, *hangAfterOversize)
	}
	log.Fatal(srv.ListenAndServeTLS("", ""))
}

type server struct {
	signingKey *rsa.PrivateKey
	caPEM      []byte
	// oversize is the set of endpoints ("discovery"/"jwks") whose responses are
	// padded past a client's max-response-size cap; empty in normal operation.
	oversize map[string]bool
}

func (s *server) handleDiscovery(w http.ResponseWriter, r *http.Request) {
	base := strings.TrimSuffix(*issuer, "/")
	doc := map[string]any{
		// Discover() requires this to exactly equal the requested issuer.
		"issuer":                                base,
		"jwks_uri":                              base + "/keys",
		"authorization_endpoint":                base + "/authorize",
		"token_endpoint":                        base + "/token",
		"response_types_supported":              []string{"id_token"},
		"subject_types_supported":               []string{"public"},
		"id_token_signing_alg_values_supported": []string{"RS256"},
		"scopes_supported":                      []string{"openid"},
		"claims_supported":                      []string{"sub", "iss", "aud", "exp", "iat"},
	}
	if s.oversize["discovery"] {
		s.writeOversizeJSON(w, r, doc)
		return
	}
	writeJSON(w, doc)
}

func (s *server) handleJWKS(w http.ResponseWriter, r *http.Request) {
	pub := s.signingKey.Public().(*rsa.PublicKey)
	jwk := map[string]any{
		"kty": "RSA",
		"use": "sig",
		"alg": "RS256",
		"kid": kid,
		"n":   b64url(pub.N.Bytes()),
		"e":   b64url(big.NewInt(int64(pub.E)).Bytes()),
	}
	if s.oversize["jwks"] {
		s.writeOversizeJSON(w, r, map[string]any{"keys": []any{jwk}})
		return
	}
	writeJSON(w, map[string]any{"keys": []any{jwk}})
}

// writeOversizeJSON emits v as JSON plus a padding field that pushes the body
// past *oversizeBytes, so a client that caps response sizes rejects it before it
// finishes reading. The result is still well-formed JSON (an ignored extra field),
// so a client WITHOUT a cap would parse it fine — the failure is purely about size.
// With -hang-after-oversize the handler then flushes and blocks until the client
// disconnects, modelling a server that keeps the stream open forever: the exact
// case that would wedge a client which drains an over-limit body instead of
// abandoning it, and which a correct client must fail fast on.
func (s *server) writeOversizeJSON(w http.ResponseWriter, r *http.Request, v map[string]any) {
	padded := make(map[string]any, len(v)+1)
	for k, val := range v {
		padded[k] = val
	}
	padded["_padding"] = strings.Repeat("A", *oversizeBytes)

	body, err := json.Marshal(padded)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write(body)
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	log.Printf("served OVERSIZED %d-byte response for %s", len(body), r.URL.Path)

	if *hangAfterOversize {
		log.Printf("holding %s connection open until the client disconnects", r.URL.Path)
		<-r.Context().Done()
		log.Printf("client disconnected from %s: %v", r.URL.Path, r.Context().Err())
	}
}

func (s *server) handleCA(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/x-pem-file")
	w.Write(s.caPEM)
}

// handleToken mints a signed JWT. Query params:
//
//	sub        overrides the subject (default "test-bot")
//	aud        overrides the audience (default -audience flag)
//	claim      repeatable key=value extra string claims (e.g. ?claim=team=infra)
//	list       repeatable key=v1,v2,... extra STRING ARRAY claims, so token rules
//	           can exercise list/set operations (e.g. ?list=groups=dev,ops)
//
// A couple of stable custom claims (org, environment) are always included so
// the sample token rules match out of the box.
func (s *server) handleToken(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	sub := valueOr(q.Get("sub"), "test-bot")
	aud := valueOr(q.Get("aud"), *defAudience)
	now := time.Now()

	claims := map[string]any{
		"iss":         strings.TrimSuffix(*issuer, "/"),
		"sub":         sub,
		"aud":         []string{aud},
		"iat":         now.Unix(),
		"nbf":         now.Unix(),
		"exp":         now.Add(*ttl).Unix(),
		"org":         "ethernet-fyi",
		"environment": "test",
	}
	for _, kv := range q["claim"] {
		if k, v, ok := strings.Cut(kv, "="); ok {
			claims[k] = v
		}
	}
	// Repeatable ?list=key=v1,v2,... injects a string ARRAY claim. JSON arrays
	// decode to []any on the Teleport side, exercising the set() flattening path
	// (e.g. contains(set(claims.groups), "dev")).
	for _, kv := range q["list"] {
		if k, v, ok := strings.Cut(kv, "="); ok {
			var arr []string
			for _, item := range strings.Split(v, ",") {
				if item = strings.TrimSpace(item); item != "" {
					arr = append(arr, item)
				}
			}
			claims[k] = arr
		}
	}

	jwt, err := s.sign(claims)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/plain")
	// No trailing newline: makes this directly usable as a tbot token command.
	fmt.Fprint(w, jwt)
}

// handleK8sToken mints a Kubernetes-style projected service-account JWT,
// suitable for testing the `kubernetes` join method (static_jwks / oidc types)
// from anywhere. Query params:
//
//	namespace       SA namespace        (default "default")
//	serviceaccount  SA name             (default "teleport-bot")
//	pod             bound pod name      (default "teleport-bot-pod")
//	aud             audience            (default -cluster-name; k8s join requires
//	                                    the Teleport cluster name as audience)
//
// The token is pod-bound (has a kubernetes.io/pod claim) and carries exp/iat
// within -ttl, as required by the JWKS/OIDC kube validators.
func (s *server) handleK8sToken(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	namespace := valueOr(q.Get("namespace"), "default")
	sa := valueOr(q.Get("serviceaccount"), "teleport-bot")
	pod := valueOr(q.Get("pod"), "teleport-bot-pod")
	aud := valueOr(q.Get("aud"), *clusterName)
	now := time.Now()

	claims := map[string]any{
		"iss": strings.TrimSuffix(*issuer, "/"),
		"sub": "system:serviceaccount:" + namespace + ":" + sa,
		"aud": []string{aud},
		"iat": now.Unix(),
		"nbf": now.Unix(),
		"exp": now.Add(*ttl).Unix(),
		"kubernetes.io": map[string]any{
			"namespace":      namespace,
			"serviceaccount": map[string]any{"name": sa, "uid": uidFor("sa/" + namespace + "/" + sa)},
			"pod":            map[string]any{"name": pod, "uid": uidFor("pod/" + namespace + "/" + pod)},
		},
	}

	jwt, err := s.sign(claims)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/plain")
	fmt.Fprint(w, jwt)
}

func (s *server) sign(claims map[string]any) (string, error) {
	header := map[string]any{"alg": "RS256", "typ": "JWT", "kid": kid}
	hb, err := json.Marshal(header)
	if err != nil {
		return "", err
	}
	cb, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	signingInput := b64url(hb) + "." + b64url(cb)
	digest := sha256.Sum256([]byte(signingInput))
	sig, err := rsa.SignPKCS1v15(rand.Reader, s.signingKey, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	return signingInput + "." + b64url(sig), nil
}

// --- key + cert persistence ---------------------------------------------

func loadOrCreateRSAKey(path string) (*rsa.PrivateKey, error) {
	if b, err := os.ReadFile(path); err == nil {
		block, _ := pem.Decode(b)
		if block == nil {
			return nil, fmt.Errorf("no PEM block in %s", path)
		}
		k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, err
		}
		return k.(*rsa.PrivateKey), nil
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, err
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600); err != nil {
		return nil, err
	}
	log.Printf("generated new signing key at %s", path)
	return key, nil
}

func loadOrCreateTLSCert() (tls.Certificate, []byte, error) {
	// Serve a provided cert (e.g. the harness wildcard LE cert) — system-trusted, so
	// the kube `oidc` join type (no custom-CA support) validates the issuer. /ca then
	// just echoes the served chain (generic_oidc relies on system trust in this mode).
	if *tlsCertFile != "" && *tlsKeyFile != "" {
		cert, err := tls.LoadX509KeyPair(*tlsCertFile, *tlsKeyFile)
		if err != nil {
			return tls.Certificate{}, nil, err
		}
		cb, err := os.ReadFile(*tlsCertFile)
		if err != nil {
			return tls.Certificate{}, nil, err
		}
		return cert, cb, nil
	}

	certPath := filepath.Join(*dataDir, "tls-cert.pem")
	keyPath := filepath.Join(*dataDir, "tls-key.pem")

	if cb, err := os.ReadFile(certPath); err == nil {
		cert, err := tls.LoadX509KeyPair(certPath, keyPath)
		if err != nil {
			return tls.Certificate{}, nil, err
		}
		return cert, cb, nil
	}

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return tls.Certificate{}, nil, err
	}

	dnsNames := []string{"localhost"}
	ips := []net.IP{net.IPv4(127, 0, 0, 1), net.IPv6loopback}
	if u, err := url.Parse(*issuer); err == nil && u.Hostname() != "" {
		if ip := net.ParseIP(u.Hostname()); ip != nil {
			ips = append(ips, ip)
		} else if u.Hostname() != "localhost" {
			dnsNames = append(dnsNames, u.Hostname())
		}
	}
	for _, s := range strings.Split(*extraSANs, ",") {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		if ip := net.ParseIP(s); ip != nil {
			ips = append(ips, ip)
		} else {
			dnsNames = append(dnsNames, s)
		}
	}

	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "teleport generic_oidc test CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(10, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		// Self-signed cert that is also its own CA, so the same PEM works as
		// both the server cert and the token's tls_ca.
		IsCA:        true,
		DNSNames:    dnsNames,
		IPAddresses: ips,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, key.Public(), key)
	if err != nil {
		return tls.Certificate{}, nil, err
	}

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})

	if err := os.WriteFile(certPath, certPEM, 0o600); err != nil {
		return tls.Certificate{}, nil, err
	}
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		return tls.Certificate{}, nil, err
	}
	log.Printf("generated new self-signed TLS cert at %s (SANs: %v %v)", certPath, dnsNames, ips)

	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	return cert, certPEM, err
}

// --- helpers ------------------------------------------------------------

func b64url(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// uidFor returns a deterministic UUID-shaped string for a name, so minted
// kubernetes tokens carry stable, realistic-looking UIDs.
func uidFor(name string) string {
	h := sha256.Sum256([]byte(name))
	x := hex.EncodeToString(h[:16])
	return fmt.Sprintf("%s-%s-%s-%s-%s", x[0:8], x[8:12], x[12:16], x[16:20], x[20:32])
}

func valueOr(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.Encode(v)
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s %s", r.RemoteAddr, r.Method, r.URL.RequestURI())
		next.ServeHTTP(w, r)
	})
}
