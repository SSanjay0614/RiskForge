# Execution role assumed by the two serverless inference endpoints. Deliberately
# narrower than the AmazonSageMakerFullAccess the console suggests: an endpoint
# needs to read one prefix each and write logs, and nothing it can do beyond that
# is useful to it.
resource "aws_iam_role" "sagemaker" {
  name = "${var.project}-sagemaker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "sagemaker.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "sagemaker_inline" {
  name = "${var.project}-sagemaker-policy"
  role = aws_iam_role.sagemaker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only, and only the two model prefixes. The endpoints have no
        # reason to see migration/ -- which is where the borrower data sits.
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.artifacts.arn}/pd-model/*",
          "${aws_s3_bucket.artifacts.arn}/lgd-model/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
        Condition = {
          StringLike = { "s3:prefix" = ["pd-model/*", "lgd-model/*"] }
        }
      },
      {
        # Container stdout/stderr and the endpoint's own invocation metrics.
        # Without this a failing container fails invisibly.
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/sagemaker/*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = ["/aws/sagemaker/Endpoints", "AWS/SageMaker"] }
        }
      },
    ]
  })
}
