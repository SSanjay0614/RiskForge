# The app host's role. Phase 12 narrowed it, because Phase 12 changed what the
# host does: it used to be the build helper that loaded the portfolio into RDS,
# and it is now a Streamlit process that starts one state machine execution and
# reads the answer out of the response.
#
# So the interface holds no database credential, no model key and no permission
# to read a query result. Ask it for a loan and it has nowhere to get one: the
# population lives under query-results/, which this role is explicitly not
# granted, and the execution output it does read carries aggregates only.

resource "aws_iam_role" "ec2" {
  name = "${var.project}-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

# Grants shell access via Session Manager -- this is what replaces the SSH key.
resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ec2_inline" {
  name = "${var.project}-ec2-policy"
  role = aws_iam_role.ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # What the interface actually does: start one execution of one state
        # machine. Named by ARN, so it cannot start any other.
        Sid      = "AskOneQuestion"
        Effect   = "Allow"
        Action   = "states:StartExecution"
        Resource = aws_sfn_state_machine.pipeline.arn
      },
      {
        # And poll that execution until it stops. A separate statement because
        # DescribeExecution is authorised against the *execution* ARN, not the
        # state machine's -- a policy that lists both actions under the machine
        # ARN starts an execution successfully and then cannot read it, which
        # presents as the UI hanging rather than as a permissions error.
        Sid      = "AndReadTheAnswer"
        Effect   = "Allow"
        Action   = "states:DescribeExecution"
        Resource = "arn:aws:states:${var.aws_region}:${data.aws_caller_identity.current.account_id}:execution:${aws_sfn_state_machine.pipeline.name}:*"
      },
      {
        # Listing only. Key names, not contents -- and the app makes no S3 call
        # at all; this is here for the operator on the box.
        Sid      = "ListTheBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        # Two prefixes, both of them build inputs: the pg_dump the portfolio was
        # loaded from and the SQL run against it. Deliberately NOT
        # query-results/* -- that prefix holds borrower rows extracted by a
        # question, and the thing rendering answers has no business reading it.
        # Not risk-results/* either: those aggregates arrive in the execution
        # output, so fetching the object would be a second path to the same
        # numbers and a second thing to keep honest.
        Sid    = "ReadBuildInputsOnly"
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.artifacts.arn}/migration/*",
          "${aws_s3_bucket.artifacts.arn}/sql/*",
        ]
      },
      {
        # Where SSM Run Command puts command output when it is configured to
        # write to S3. Write-only, one prefix.
        Sid      = "WriteSsmCommandOutput"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/ssm-output/*"
      },
    ]
  })
}

# Removed in Phase 12, and recorded here rather than deleted silently: this role
# used to hold secretsmanager:GetSecretValue on the RDS master secret, from when
# the instance was the host that loaded the portfolio. The Streamlit app never
# opens a database connection, so the grant was standing access to the master
# credential in aid of nothing.
#
# A future reload from this box needs it back. That is this block, uncommented,
# plus one apply:
#
#   {
#     Sid      = "ReadTheDatabaseMasterSecret"
#     Effect   = "Allow"
#     Action   = ["secretsmanager:GetSecretValue"]
#     Resource = aws_db_instance.main.master_user_secret[0].secret_arn
#   },
#
# Better still, connect as riskforge_ro with IAM authentication and read nothing
# at all -- see Deploy/lambda/sql/create_readonly_role.sql.

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project}-ec2-profile"
  role = aws_iam_role.ec2.name
}
