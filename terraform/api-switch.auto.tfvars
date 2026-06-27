# Master on/off switch for the public API (auto-loaded by terraform).
#
#   api_enabled = false  ->  API OFF. The Lambda's reserved concurrency is set to 0,
#                            so API Gateway cannot invoke it. Wrong-token and random
#                            internet requests run no code and touch no DynamoDB, so
#                            the arena costs near zero while nobody is playing. The
#                            viewer still loads but its data calls return errors.
#   api_enabled = true   ->  API ON. Normal play.
#
# To toggle: edit the line below, then `scripts/deploy.sh` (or
# `terraform -chdir=terraform apply -auto-approve`). Committed on purpose so the
# current on/off state is visible in the repo.
api_enabled = true
