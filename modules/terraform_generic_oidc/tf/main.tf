# terraform_generic_oidc: create a generic_oidc join token whose spec sets
# `must_match_fields`. addr + identity come from TF_TELEPORT_* env on the runner.
#
# `must_match_fields` is the field the provider currently DROPS: it's excluded from the
# generated schema (google.protobuf.Struct is unsupported by protoc-gen-terraform), so it has
# no schema attribute / no CopyFrom. `terraform apply` SUCCEEDS but silently omits the field —
# the created token has must_match_fields=null. Everything else here is valid, so once the
# provider bug is fixed this token is created carrying must_match_fields. (The exact HCL shape
# of the value — nested map vs. jsonencode(...) string — is finalized alongside the fix.)
#
# org=ethernet-fyi matches the claim the oidc-server always mints, so the positive join agent
# satisfies must_match_fields while the wrong-org agent does not (see the module's join test).
terraform {
  required_providers {
    teleport = {
      source = "terraform.releases.teleport.dev/gravitational/teleport"
    }
  }
}

provider "teleport" {}

resource "teleport_provision_token" "oidc" {
  version = "v2"
  metadata = {
    name = "tf-oidc-token"
  }
  spec = {
    roles       = ["Node"]
    join_method = "generic_oidc"
    generic_oidc = {
      issuer   = var.issuer
      audience = var.audience

      # Global AND-matched fields — currently dropped by the provider (schema-excluded).
      must_match_fields = {
        org = "ethernet-fyi"
      }

      # OR-matched rules; at least one must pass. sub=tf-oidc-subject is what the join-test
      # agents present, so both satisfy allow_any and differ only in `org`.
      allow_any = [{
        conditions = [{
          attribute = "sub"
          eq        = { value = "tf-oidc-subject" }
        }]
      }]
    }
  }
}
