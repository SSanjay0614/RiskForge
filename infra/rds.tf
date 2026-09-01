# The data tier: plain Amazon RDS for PostgreSQL, not Aurora Serverless v2.
# The reasoning is written up in Docs/README_data_tier.md -- in short, this is a
# single-writer, read-mostly ~1 GiB analytical snapshot, so none of what Aurora
# charges for is exercised, and Aurora on this account plan is only creatable
# through express configuration, which puts the cluster outside the VPC and
# forces IAM-only auth. Choosing RDS kept the database private.

# A DB subnet group needs subnets in at least two AZs even for a Single-AZ
# instance: AWS requires the failover surface to exist before it is used.
resource "aws_db_subnet_group" "db" {
  name        = "${var.project}-db-subnets"
  description = "RiskForge PostgreSQL -- default VPC subnets, us-east-1a/b/c"
  subnet_ids  = data.aws_subnets.db.ids
}

# The default parameter group cannot be edited, and TLS is not something to
# leave to whichever client happens to connect. rds.force_ssl makes the server
# itself reject any non-TLS connection, so neither the app host nor a Lambda
# can send credentials in the clear even if its connection string forgets to
# ask for encryption.
resource "aws_db_parameter_group" "postgres" {
  name        = "${var.project}-pg16"
  family      = "postgres16"
  description = "RiskForge: require TLS, log slow queries"

  # Stated explicitly even though it is redundant: rds.force_ssl already defaults
  # to 1 for the postgres16 family, which is why the API reports this entry with
  # Source "system" rather than as a user modification. Keeping it in code means
  # the guarantee is reviewable here instead of resting on an AWS default that
  # was 0 in older families and could move again.
  #
  # apply_method stays pending-reboot to match what RDS reports back. The
  # parameter is genuinely dynamic (ApplyType "dynamic" in the engine defaults),
  # but because the value equals the system default RDS does not track it as
  # user-set, so asking for "immediate" produces a diff on every single plan that
  # applying never resolves.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Anything past a second is worth seeing. The retrieval queries scan ~878k
  # joined rows, so this threshold catches real regressions rather than noise.
  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = "riskforge_admin"

  # RDS generates the password and stores it in Secrets Manager itself, so it
  # never passes through Terraform state, this repo, or a terminal transcript.
  # The EC2 role is granted GetSecretValue on this one secret ARN and nothing
  # else -- see iam_ec2.tf.
  manage_master_user_password = true

  # gp3 at its 20 GiB floor rather than gp2 at 5 GiB. gp2 baseline IOPS scale
  # with volume size, so 5 GiB would allocate 15 baseline IOPS; gp3 gives 3,000
  # flat at any size in this range. Every retrieval query is a full scan of the
  # 878k-row join, which is precisely the workload that notices. The difference
  # is about $1.70/month for roughly 200x the baseline IOPS.
  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  # Storage autoscaling deliberately off. The dataset is a fixed snapshot, so
  # growth would mean a bug or a runaway load, and silently growing the volume
  # on a credit budget hides that instead of surfacing it.
  max_allocated_storage = 0

  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  # Not reachable from the internet: no public address, and the security group
  # admits only the app host and the VPC-attached Lambdas by SG reference.
  publicly_accessible = false
  port                = 5432

  # Lets a caller authenticate with a short-lived, SigV4-derived token instead of
  # a password. Granted to exactly one database role (riskforge_ro, SELECT on
  # three tables) and to no one else, so the Lambdas hold no credential, there is
  # nothing for them to rotate, and IAM -- not a password in an environment
  # variable -- decides who may connect. The master user still uses the
  # RDS-managed password above; this adds a path, it does not replace one.
  iam_database_authentication_enabled = true

  # Single-AZ is a costed decision, not an oversight. The contents are a static
  # snapshot restorable from S3 in minutes, so the RPO a hot standby buys is not
  # worth doubling the instance bill. `multi_az = true` is the whole change if
  # this ever carried anything that could not be rebuilt.
  multi_az = false

  # Windows are UTC and sit in the small hours of IST so a backup never lands
  # mid-demo. They must not overlap each other. Retention is capped by the free
  # account plan rather than chosen -- see variables.tf.
  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "19:30-20:00"
  maintenance_window         = "sun:20:30-sun:21:30"
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true
  apply_immediately          = true

  # Postgres logs go to CloudWatch, so slow queries and rejected connections
  # are visible without opening an SSM session onto the instance.
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  # Demo scope: both would be the opposite in anything real. Set this way so
  # `terraform destroy` completes and leaves nothing billable behind.
  skip_final_snapshot = true
  deletion_protection = false
}
