# The one guard rail that cannot be added retroactively: a runaway fan-out is otherwise
# discovered by the bill. It alarms; it does not cap - AWS does not stop work at a budget.
#
# Scoped by the Deployment tag, which every resource in both roots carries, so the figure is this
# deployment's and not the account's.
resource "aws_budgets_budget" "deployment" {
  count = var.budget_usd > 0 ? 1 : 0

  name         = "stratum-${var.deployment}"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Deployment$${var.deployment}"]
  }

  # 80 % of what has been spent, and 100 % of what is forecast. The forecast one is the useful
  # one: it fires while there is still something to do about it.
  dynamic "notification" {
    for_each = var.budget_alert_email == "" ? [] : [
      { type = "ACTUAL", threshold = 80 },
      { type = "FORECASTED", threshold = 100 },
    ]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
