"""
The container's own code: the parts of the risk pipeline that could not be
reused from the repository as they are.

Everything else is imported from the copied repository modules -- see
Deploy/fargate/stage.py, which lists exactly what lands in the image and why.

  inputs.py    reads the query result CSV that riskforge-execute-sql wrote to S3
  features.py  runs the copied FeatureEngineeringTool and checks its output
  scoring.py   replaces ExpectedLossTool's joblib.load with two SageMaker calls
  credit.py    the CreditRiskAgent branch: features, scoring, Expected Loss
  rates.py     the InterestRateConcentrationAgent branch: repricing gap and HHI
  outputs.py   writes aggregates to S3, and refuses to write anything per-loan
"""
