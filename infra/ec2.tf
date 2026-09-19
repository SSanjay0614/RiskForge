resource "aws_instance" "app" {
  ami           = data.aws_ssm_parameter.al2023.value
  instance_type = var.ec2_instance_type

  subnet_id                   = data.aws_subnets.db.ids[0]
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = true

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  # IMDSv2 only -- blocks the SSRF-to-credential-theft path that IMDSv1 allows.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  user_data = <<-EOF
    #!/bin/bash
    dnf update -y
    dnf install -y python3 python3-pip git postgresql16 sqlite
  EOF

  tags = { Name = "${var.project}-app" }

  # The AMI ID moves whenever Amazon publishes a new AL2023 build; without this
  # every future plan would want to destroy and recreate the instance.
  lifecycle {
    ignore_changes = [ami]
  }
}

# A permanent address for the app.
#
# The auto-assigned public IPv4 above is borrowed for the life of the running
# instance: a stop/start hands back a different one, and so does a replacement.
# That is fine for a host you reach through SSM and wrong for a link sent to
# somebody in advance, because the link stops resolving without anything having
# failed. An Elastic IP is held by the account rather than the instance, so the
# address outlives a stop, a start and a rebuild.
#
# Cost, accurately: every public IPv4 address in AWS bills at $0.005/hour since
# February 2024 -- attached or not, Elastic or auto-assigned. The instance is
# already paying that for the address it borrowed, so attaching this one is a
# swap rather than an addition. The difference is that an EIP keeps billing while
# the instance is stopped, where the borrowed address would have been released;
# releasing the EIP is the way to stop that, and it gives up the fixed address.
#
# Attaching REPLACES the auto-assigned address, so the public IP and the
# ec2-<ip>.compute-1.amazonaws.com hostname both change once, at this apply, and
# then stop moving.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = { Name = "${var.project}-app" }
}
