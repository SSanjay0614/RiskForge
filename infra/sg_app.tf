# App host / build helper. No port 22: shell access goes through SSM Session
# Manager instead, so there is no SSH key to store, no .pem to leak into a
# public repo, and no inbound port open to the internet at all.
resource "aws_security_group" "app" {
  name        = "${var.project}-sg"
  description = "RiskForge app host: Streamlit from my IP only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "app_streamlit" {
  security_group_id = aws_security_group.app.id
  description       = "Streamlit UI"
  cidr_ipv4         = var.my_ip_cidr
  from_port         = 8501
  to_port           = 8501
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "Outbound: SSM, yum, S3, Bedrock, SageMaker"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Lambda functions that need Aurora. Egress only -- nothing calls a Lambda
# through its own security group.
resource "aws_security_group" "lambda" {
  name        = "${var.project}-lambda-sg"
  description = "RiskForge VPC-attached Lambdas"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "lambda_all" {
  security_group_id = aws_security_group.lambda.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
