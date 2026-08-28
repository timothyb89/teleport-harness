# The smallest apply that proves the provider got a working client: create a bot and its
# join token. Deliberately the same resources as modules/terraform_bot, so a failure here
# is about HOW the provider authenticated, never about what it was asked to create.
#
# The provider block is empty — addr and the credential source both come from the
# TF_TELEPORT_* environment each runner sets, which is the surface the customer uses.
terraform {
  required_providers {
    teleport = {
      source = "terraform.releases.teleport.dev/gravitational/teleport"
    }
  }
}

provider "teleport" {}

resource "teleport_provision_token" "probe" {
  version = "v2"
  metadata = {
    # `-probe-` deliberately: <prefix>-token is the runner's own kubernetes JOIN token,
    # created at bootstrap, and creating it again here would overwrite the thing this
    # runner authenticates with.
    name = "${var.name_prefix}-probe-token"
  }
  spec = {
    roles       = ["Bot"]
    bot_name    = "${var.name_prefix}-probe-bot"
    join_method = "token"
  }
}

resource "teleport_bot" "probe" {
  version = "v1"
  metadata = {
    name = "${var.name_prefix}-probe-bot"
  }
  spec = {
    roles = ["access"]
  }

  depends_on = [teleport_provision_token.probe]
}
