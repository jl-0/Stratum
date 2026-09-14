# The EDL credential CONTAINER only. The VALUE is never created here: anything passed through
# Terraform lands in state, and state lives in the backend bucket. Populate out of band with
# scripts/put-edl-secret.sh (playbook section 7 rule 2).
#
# There is deliberately no aws_secretsmanager_secret_version resource. Its absence is the
# mechanism - nothing to ignore_changes, because nothing is ever written from here.
resource "aws_secretsmanager_secret" "edl" {
  name        = "stratum/${var.deployment}/earthdata"
  description = "Earthdata Login credential. Value set out of band, never by Terraform."
}
