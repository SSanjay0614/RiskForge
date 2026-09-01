# One execution role per function, not one shared role. The two functions need
# different things -- only execute-sql writes to S3 -- and a shared role would
# grant each of them the union.
#
# Neither role can read a secret. Both authenticate to PostgreSQL with an RDS IAM
# auth token, so there is no password for them to fetch, and the rds-db:connect
# grant below is scoped to one database user: even with this role, nothing can
# connect as riskforge_admin.

locals {
  lambda_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  # The resource ARN for IAM database authentication uses the instance's
  # *resource id* (db-ABC123...), not its identifier -- an easy and silent
  # mistake, because a policy naming the identifier is valid IAM and simply
  # never matches.
  rds_connect_arn = format(
    "arn:aws:rds-db:%s:%s:dbuser:%s/%s",
    var.aws_region,
    data.aws_caller_identity.current.account_id,
    aws_db_instance.main.resource_id,
    var.db_readonly_user
  )
}

resource "aws_iam_role" "lambda_execute_sql" {
  name               = "${var.project}-execute-sql-role"
  assume_role_policy = local.lambda_assume_role
}

resource "aws_iam_role" "lambda_compliance_check" {
  name               = "${var.project}-compliance-check-role"
  assume_role_policy = local.lambda_assume_role
}

# Creates and deletes the ENI a VPC-attached function runs behind. This is the
# one place a managed policy is the right answer: the actions are ec2:*
# NetworkInterface calls that cannot be scoped to a resource anyway.
resource "aws_iam_role_policy_attachment" "execute_sql_vpc" {
  role       = aws_iam_role.lambda_execute_sql.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "compliance_check_vpc" {
  role       = aws_iam_role.lambda_compliance_check.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "execute_sql_inline" {
  name = "${var.project}-execute-sql-policy"
  role = aws_iam_role.lambda_execute_sql.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConnectAsReadOnlyUserOnly"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = local.rds_connect_arn
      },
      {
        # Write results only, and only under the prefix the bucket expires after
        # 7 days. No GetObject: this function produces query results, it never
        # reads back the migration data sitting in the same bucket.
        # AbortMultipartUpload is separate from PutObject and is what stops a
        # failed run leaving billed, invisible parts behind.
        Sid      = "WriteQueryResults"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.artifacts.arn}/${var.query_results_prefix}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "compliance_check_inline" {
  name = "${var.project}-compliance-check-policy"
  role = aws_iam_role.lambda_compliance_check.id

  # Nothing but the database connection. This function reads five threshold rows
  # and does arithmetic; it has no reason to touch S3 at all.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ConnectAsReadOnlyUserOnly"
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = local.rds_connect_arn
    }]
  })
}
