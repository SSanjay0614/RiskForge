provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  # Tagging everything lets Cost Explorer break spend down by project, which
  # is the only practical way to answer "what is costing me money" later.
  default_tags {
    tags = {
      Project   = "RiskForge"
      ManagedBy = "Terraform"
    }
  }
}
