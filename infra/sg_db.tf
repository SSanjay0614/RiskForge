# PostgreSQL is reachable only from the two security groups below -- never from
# an IP range, and never from the internet (publicly_accessible = false). Rules
# reference the source security group rather than a CIDR, so they keep working
# when the app host is replaced and its private address changes.
resource "aws_security_group" "db" {
  name        = "${var.project}-db-sg"
  description = "RiskForge PostgreSQL: from app host and Lambdas only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the app host"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_lambda" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from VPC-attached Lambdas"
  referenced_security_group_id = aws_security_group.lambda.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
