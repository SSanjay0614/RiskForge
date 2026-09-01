resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project}-artifacts-${data.aws_caller_identity.current.account_id}"
}

# Public access stays fully blocked: this holds model artifacts and query
# result sets, none of which has any reason to be internet-readable.
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 rather than KMS: free, and there is no compliance requirement here
# that a customer-managed key would satisfy.
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Query result sets are intermediate data -- reproducible by re-running the
# pipeline, so paying to store old versions of them makes no sense.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-query-results"
    status = "Enabled"

    filter {
      prefix = "query-results/"
    }

    expiration {
      days = 7
    }
  }
}
