# Deployment

The arena runs as a scale-to-zero serverless stack. Idle cost is approximately
zero; active cost is proportional to requests. Everything that holds data or runs
code is in **Switzerland (`eu-central-2`, Zurich)**. The only resources outside that
region are the CloudFront distributions (a global CDN) and their TLS certificate,
which must be in `us-east-1`; that certificate is public and holds no data, and the
files CloudFront serves are public static assets.

## Topology

| Hostname | Serves | AWS resources |
|---|---|---|
| `ludicrous-arena.com`, `www` | spectator viewer | S3 (eu-central-2) + CloudFront, cert in us-east-1 |
| `api.ludicrous-arena.com` | the game API | Lambda + API Gateway HTTP API (eu-central-2), cert in eu-central-2 |
| `docs.ludicrous-arena.com` | static docs site | S3 (eu-central-2) + CloudFront, cert in us-east-1 |

State lives in DynamoDB (`arena`, on-demand) in eu-central-2. Terraform state is in
the private, versioned bucket `arena-tfstate-ACCOUNT_ID` (eu-central-2). A
CloudWatch billing alarm at 5 USD is in us-east-1 (billing metrics exist only there).

## First-time setup

```bash
scripts/bootstrap-state.sh        # create the Terraform state bucket (once)
terraform -chdir=terraform init   # uses the S3 backend
```

`terraform/terraform.tfvars` carries the per-deployment values (region, domain,
alert email). It is gitignored.

## Phase 1: base stack (no custom domains)

Brings the API and viewer up on the default AWS URLs. Independent of DNS.

```bash
scripts/package-lambda.sh
terraform -chdir=terraform apply
scripts/deploy-frontend.sh        # sync viewer to S3 + invalidate CloudFront
```

Outputs `api_url`, `viewer_url`, and the Route53 `nameservers`.

## DNS delegation (manual, at the registrar)

The domain is registered at Namecheap. In Namecheap, set the domain's nameservers
to **Custom DNS** with the four servers from `terraform output nameservers`. Verify
the delegation reached the registry before applying Phase 2:

```bash
dig +norecurse +noall +authority NS ludicrous-arena.com. @a.gtld-servers.net.
# wait until this lists the awsdns-* servers, stably
```

## Phase 2: custom domains + docs site

Requires delegation to be live, ACM validates via the Route53 zone. With
`domain_name` set in tfvars, the same `apply` adds two certificates, the API Gateway
and docs custom domains, the docs site, and all the Route53 records.

```bash
terraform -chdir=terraform apply
scripts/deploy-docs.sh            # build + sync the docs site
```

## Issuing access tokens

Players need a token. Mint one against the deployed table (only its hash is stored):

```bash
ARENA_TABLE=arena AWS_DEFAULT_REGION=eu-central-2 \
  backend/.venv/bin/python scripts/issue-token.py --user alice --name "Alice"
```

Hand the plaintext token to the player out of band. Revoke by deleting the `TOKEN#`
item (or setting `revoked=true`) in DynamoDB.

## Day-to-day

- Backend code change: `scripts/deploy-backend.sh` (repackages and updates the Lambda).
- Viewer change: `scripts/deploy-frontend.sh`.
- Docs change: `scripts/deploy-docs.sh`.
- Full pipeline: `scripts/deploy.sh`.
