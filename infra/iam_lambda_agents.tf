# One execution role per prompt function, even though all three policies are
# identical today. The cost of three roles is three lines of HCL; the cost of one
# shared role is that the day one of them needs something extra -- a DynamoDB
# table for prompt caching, an S3 prefix for transcripts -- all three get it, and
# nobody notices because nothing breaks.
#
# What these roles can do is read one SSM parameter and write their own logs.
# They cannot reach the database: no rds-db:connect, no VPC access, and the
# functions have no network path to the private subnets. The read-only boundary
# is enforced by PostgreSQL for riskforge-execute-sql, which is the only function
# that runs generated SQL at all -- riskforge-sqlgen-action only writes the text.

resource "aws_iam_role" "agent" {
  for_each = local.agent_functions

  name               = "${var.project}-${each.value.name}-role"
  assume_role_policy = local.lambda_assume_role
}

# The basic policy, not the VPC one: these functions have no ENI to manage, and
# AWSLambdaVPCAccessExecutionRole would hand them ec2:CreateNetworkInterface for
# no reason.
resource "aws_iam_role_policy_attachment" "agent_basic" {
  for_each = local.agent_functions

  role       = aws_iam_role.agent[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "agent_inline" {
  for_each = local.agent_functions

  name = "${var.project}-${each.value.name}-policy"
  role = aws_iam_role.agent[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # One parameter by ARN, not /riskforge/* and not a wildcard. This role
        # has no business reading any other parameter this project ever adds.
        Sid      = "ReadTheModelApiKey"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.gemini_api_key.arn
      },
      {
        # WithDecryption=true is an SSM call that turns into a KMS call under the
        # hood, so ssm:GetParameter alone returns an AccessDenied from KMS.
        #
        # The condition is what keeps this from being a general-purpose decrypt
        # grant: the key is the account's aws/ssm alias, whose ARN is not known
        # until the account has one, so the resource is "*" -- and ViaService
        # narrows it back down to "only when SSM is the one asking". A caller
        # holding this role cannot use it to decrypt anything else the aws/ssm
        # key protects through some other path.
        Sid      = "DecryptOnlyViaSsm"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      },
    ]
  })
}
