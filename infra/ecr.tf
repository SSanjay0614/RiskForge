# The image the two risk agents run as. One repository, one image, two modes --
# see Deploy/fargate/Dockerfile for why the mode is an argument rather than two
# images.
#
# Terraform owns the repository; Deploy/fargate/build_and_push.py refuses to
# create it and tells you to apply this file instead. That is deliberate: a
# repository created by the build script would be a resource Terraform does not
# know about, and the first `terraform apply` after it would try to create it
# again.
resource "aws_ecr_repository" "risk" {
  name = "${var.project}-risk"

  # MUTABLE because `latest` is re-pointed on every push, and the task
  # definition below tracks a tag. Every push also carries an immutable
  # content tag (the build's manifest digest) so a specific build stays
  # addressable after `latest` has moved on.
  image_tag_mutability = "MUTABLE"

  # Basic scanning, which is free. It reads the OS package list in the image
  # against the CVE feed -- so it finds a stale libssl in the python:3.9-slim
  # base, which is exactly the thing that goes stale without anyone noticing.
  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  # A repository with images in it cannot be deleted, so without this
  # `terraform destroy` fails partway through and leaves the rest of the stack
  # half-torn-down. The images are rebuildable from the repository in one
  # command, so there is nothing here worth protecting with a manual step.
  force_delete = true
}

# Storage is billed per GB-month and this image is around 1 GB, so untagged
# layers left behind by repeated pushes are a slow leak rather than a risk.
# Untagged goes after a day; tagged keeps the last five, which is enough to roll
# back to a build that worked.
resource "aws_ecr_lifecycle_policy" "risk" {
  repository = aws_ecr_repository.risk.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the five most recent tagged images"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 5
        }
        action = { type = "expire" }
      },
    ]
  })
}
