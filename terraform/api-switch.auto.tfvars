# Master on/off switch for the public API (auto-loaded by terraform).
#
#   api_enabled = false  ->  API OFF. The FC function's environment carries
#                            API_ENABLED=false and the handler returns 503 for
#                            every API call, so wrong-token and random internet
#                            requests run no game code and touch no Tablestore,
#                            keeping cost near zero while nobody is playing.
#                            The viewer still loads but its data calls return errors.
#   api_enabled = true   ->  API ON. Normal play.
#
# To toggle: edit the line below, then `scripts/deploy.sh` (or
# `terraform -chdir=terraform apply -auto-approve`). Committed on purpose so the
# current on/off state is visible in the repo.
api_enabled = false
