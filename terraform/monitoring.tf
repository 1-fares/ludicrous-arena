# Cost guard rail: a CloudWatch billing alarm. Billing metrics only exist in
# us-east-1, so the alarm and its SNS topic use the us_east_1 provider alias. The
# alarm is a metric on estimated charges; it holds no application data. Enabled
# only when alert_email is set.
resource "aws_sns_topic" "billing" {
  count    = var.alert_email == "" ? 0 : 1
  provider = aws.us_east_1
  name     = "${local.name}-billing-alerts"
  tags     = local.tags
}

resource "aws_sns_topic_subscription" "billing" {
  count     = var.alert_email == "" ? 0 : 1
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.billing[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "billing" {
  count               = var.alert_email == "" ? 0 : 1
  provider            = aws.us_east_1
  alarm_name          = "${local.name}-monthly-cost"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 21600 # 6h; billing metric updates a few times a day
  statistic           = "Maximum"
  threshold           = 5
  alarm_description   = "Estimated monthly AWS charges exceeded 5 USD"
  dimensions          = { Currency = "USD" }
  alarm_actions       = [aws_sns_topic.billing[0].arn]
  tags                = local.tags
}
