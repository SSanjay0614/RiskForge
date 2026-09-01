# The PD and LGD models, served as two SageMaker Serverless Inference endpoints.
#
# Serverless rather than real-time is the whole cost story here: a real-time
# endpoint bills for its instance every hour it exists, whether or not anything
# calls it, and forgetting one running overnight is the single most expensive
# mistake available in this build. Serverless bills per request plus duration and
# nothing at all while idle, which matches a demo that runs in bursts. The
# trade-off it buys back with is cold-start latency on the first call after a
# quiet period -- acceptable for an analyst-facing tool, and not acceptable for
# anything with a latency SLA.
#
# The artifacts are built and proven identical to the notebook models by
# Deploy/sagemaker/build_artifacts.py before they are uploaded.

locals {
  # SageMaker's prebuilt XGBoost images are published per-region under an
  # AWS-owned account, so this ID is us-east-1's and would change with the region.
  sagemaker_xgboost_image = "683313688378.dkr.ecr.${var.aws_region}.amazonaws.com/sagemaker-xgboost:${var.sagemaker_xgboost_version}"

  # Framework ("script") mode: the container runs the handler shipped inside
  # model.tar.gz under code/ instead of its own default XGBoost server. These are
  # the variables the SageMaker Python SDK would otherwise set for us -- spelled
  # out because this stack is Terraform, not the SDK.
  sagemaker_script_env = {
    SAGEMAKER_PROGRAM             = "inference.py"
    SAGEMAKER_SUBMIT_DIRECTORY    = "/opt/ml/model/code"
    SAGEMAKER_CONTAINER_LOG_LEVEL = "20"
    SAGEMAKER_REGION              = var.aws_region
  }
}

resource "aws_sagemaker_model" "pd" {
  name               = "${var.project}-pd-model"
  execution_role_arn = aws_iam_role.sagemaker.arn

  primary_container {
    image          = local.sagemaker_xgboost_image
    model_data_url = "s3://${aws_s3_bucket.artifacts.id}/pd-model/model.tar.gz"
    environment    = local.sagemaker_script_env
  }
}

resource "aws_sagemaker_model" "lgd" {
  name               = "${var.project}-lgd-model"
  execution_role_arn = aws_iam_role.sagemaker.arn

  primary_container {
    image          = local.sagemaker_xgboost_image
    model_data_url = "s3://${aws_s3_bucket.artifacts.id}/lgd-model/model.tar.gz"
    environment    = local.sagemaker_script_env
  }
}

# A new endpoint configuration is required for every model change -- the resource
# is immutable server-side. create_before_destroy lets the endpoint be updated to
# point at the replacement before the old config goes away, instead of the
# endpoint briefly referencing nothing.
resource "aws_sagemaker_endpoint_configuration" "pd" {
  name = "${var.project}-pd-config"

  production_variants {
    variant_name = "AllTraffic"
    model_name   = aws_sagemaker_model.pd.name

    serverless_config {
      memory_size_in_mb = var.sagemaker_memory_mb
      max_concurrency   = var.sagemaker_max_concurrency
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_sagemaker_endpoint_configuration" "lgd" {
  name = "${var.project}-lgd-config"

  production_variants {
    variant_name = "AllTraffic"
    model_name   = aws_sagemaker_model.lgd.name

    serverless_config {
      memory_size_in_mb = var.sagemaker_memory_mb
      max_concurrency   = var.sagemaker_max_concurrency
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_sagemaker_endpoint" "pd" {
  name                 = "${var.project}-pd-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.pd.name
}

resource "aws_sagemaker_endpoint" "lgd" {
  name                 = "${var.project}-lgd-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.lgd.name
}
