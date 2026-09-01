terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State lives OUTSIDE the OneDrive-synced repo on purpose: it records the
  # Aurora master password in plaintext, and OneDrive syncing a file that
  # Terraform rewrites mid-apply risks corrupting it.
  backend "local" {
    path = "C:/tf-state/riskforge/terraform.tfstate"
  }
}
