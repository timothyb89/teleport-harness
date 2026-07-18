variable "issuer" {
  type        = string
  description = "OIDC issuer URL written into the token (not fetched by this test)."
}

variable "audience" {
  type        = string
  description = "Expected audience value for the generic_oidc token."
}
