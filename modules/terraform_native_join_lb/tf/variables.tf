# Each runner applies this same config with its own prefix (TF_VAR_name_prefix), so the
# resources it creates name the runner that created them and `resource_present` can tell
# a successful apply from one that never got a client.
variable "name_prefix" {
  type        = string
  description = "Prefix for the resources this runner creates."
}
