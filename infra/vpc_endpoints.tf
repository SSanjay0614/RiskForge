# The only VPC endpoint this build needs, and it is free.
#
# A VPC-attached Lambda gets an ENI with no public address, so it cannot use the
# default VPC's internet gateway even though the subnets are public. That leaves
# it with no route to S3 -- and execute-sql writes every result set to S3. The
# usual answers are a NAT gateway (~$32/month plus data processing) or an S3
# interface endpoint (~$7/month). A *gateway* endpoint is neither: it is a route
# table entry, it costs nothing per hour and nothing per GB, and the traffic
# never touches the internet.
#
# Two things the functions notably do NOT need an endpoint for, which is why the
# list stops here:
#   * Secrets Manager -- because nothing reads a secret. IAM auth tokens are
#     computed locally by SigV4-signing with the role's own credentials, no API
#     call involved. This is the second time that choice paid for itself.
#   * CloudWatch Logs -- the Lambda service ships a function's logs out of band,
#     not over the function's own network interface.

data "aws_route_tables" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"

  # Deliberately left at full access rather than scoped to the artifacts bucket.
  # A gateway endpoint applies to every instance in the VPC that routes through
  # it, and Amazon Linux serves its package repositories from S3 -- a policy
  # naming only this bucket would silently break `dnf install` on the app host.
  # Access to the bucket is already controlled where it belongs: on the bucket's
  # public access block and on each role's IAM policy.

  tags = { Name = "${var.project}-s3-gateway" }
}

resource "aws_vpc_endpoint_route_table_association" "s3" {
  for_each = toset(data.aws_route_tables.default.ids)

  vpc_endpoint_id = aws_vpc_endpoint.s3.id
  route_table_id  = each.value
}
