# terraform_bot: create a Machine ID bot + its join token via the Teleport provider.
# addr + identity come from TF_TELEPORT_* env set on the runner container, so the
# provider block is empty and this config is portable (works standalone too).
terraform {
  required_providers {
    teleport = {
      source = "terraform.releases.teleport.dev/gravitational/teleport"
    }
  }
}

provider "teleport" {}

# The join token the bot would use to join (classic token method; name == secret).
resource "teleport_provision_token" "demo" {
  version = "v2"
  metadata = {
    name = "tf-demo-token"
  }
  spec = {
    roles       = ["Bot"]
    bot_name    = "tf-demo-bot"
    join_method = "token"
  }
}

# The bot itself (new-schema: metadata + spec).
resource "teleport_bot" "demo" {
  version = "v1"
  metadata = {
    name = "tf-demo-bot"
  }
  spec = {
    roles = ["access"]
  }

  depends_on = [teleport_provision_token.demo]
}
