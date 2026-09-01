data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

# A DB subnet group needs subnets in at least two AZs. Pinned to a/b/c rather
# than "all default subnets" because us-east-1e has historically lacked capacity
# for some instance families, and a subnet group containing it can fail at
# creation time for reasons that have nothing to do with this config.
data "aws_subnets" "db" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "availability-zone"
    values = ["us-east-1a", "us-east-1b", "us-east-1c"]
  }
}

# Amazon Linux 2023, resolved at plan time so we never hardcode an AMI ID
# that goes stale.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
