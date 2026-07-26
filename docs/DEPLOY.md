# Deployment

The arena runs as a scale-to-zero serverless stack on Alibaba Cloud (Aliyun).
Idle cost is approximately zero; active cost is proportional to requests.
Everything that holds data or runs code is in **Singapore (`ap-southeast-1`)**.

## Topology

| Hostname | Serves | Aliyun resources |
|---|---|---|
| `ludicrous-arena.com` | spectator viewer + API | FC 3.0 function (bundled frontend + FastAPI) |
| `api.ludicrous-arena.com` | the game API | same FC function, second custom domain |

State lives in Tablestore (OTS, instance `arena-<suffix>`) in ap-southeast-1.
Terraform state is in the private, versioned OSS bucket `arena-tfstate-aliyun`.

## First-time setup

```bash
scripts/bootstrap-state.sh        # create the Terraform state OSS bucket (once)
terraform -chdir=terraform init   # uses the OSS backend
```

`terraform/terraform.tfvars` carries the per-deployment values (region, domain,
cert paths). Copy from `terraform.tfvars.example` and adjust. It is gitignored.

## Phase 1: base stack (no custom domains)

Brings the API and viewer up on the default FC URL. Independent of DNS.

```bash
scripts/package-fc.sh
terraform -chdir=terraform apply
```

Outputs `fc_url` (the HTTP trigger URL). The viewer is at the same URL (open `/`).

## DNS + HTTPS (manual, Phase 2)

Requires a domain you control. The FC custom domain needs:

1. **CNAME records** at your DNS provider:
   ```
   ludicrous-arena.com      CNAME  <account-id>.<region>.fc.aliyuncs.com
   api.ludicrous-arena.com  CNAME  <account-id>.<region>.fc.aliyuncs.com
   ```
   (The exact CNAME target is shown in the FC console under your function's
   custom domain, or use `<account-id>.<region>.fc.aliyuncs.com`.)

2. **TLS certificate** (RSA 2048+, not ECC — FC rejects ECDSA keys):
   ```bash
   acme.sh --issue -d ludicrous-arena.com -d api.ludicrous-arena.com \
     --dns --keylength 2048
   # Convert key to PKCS#8 if needed:
   openssl pkcs8 -topk8 -nocrypt -in key.pem -out key.pk8.pem
   ```

3. **Set in tfvars** and re-apply:
   ```hcl
   domain_name = "ludicrous-arena.com"
   cert_path   = "/path/to/fullchain.pem"
   key_path    = "/path/to/key.pk8.pem"
   ```
   ```bash
   terraform -chdir=terraform apply
   ```

## Issuing access tokens

Players need a token. Mint one against the deployed table (only its hash is stored):

```bash
ARENA_TABLE=$(terraform -chdir=terraform output -raw ots_table) \
OTS_ENDPOINT="https://<instance>.ap-southeast-1.ots.aliyuncs.com" \
OTS_INSTANCE="<instance>" \
ALIBABA_CLOUD_ACCESS_KEY_ID="..." \
ALIBABA_CLOUD_ACCESS_KEY_SECRET="..." \
  backend/.venv/bin/python scripts/issue-token.py --user alice --name "Alice"
```

Hand the plaintext token to the player out of band. Revoke by deleting the
`TOKEN#` row (or setting `revoked=true`) in Tablestore.

## Day-to-day

- Backend code change: `scripts/deploy-backend.sh` (repackages and updates the FC function).
- Full pipeline: `scripts/deploy.sh` (tests + package + terraform apply).
- Toggle the API on/off: edit `terraform/api-switch.auto.tfvars`, then `scripts/deploy.sh`.
