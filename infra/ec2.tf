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
