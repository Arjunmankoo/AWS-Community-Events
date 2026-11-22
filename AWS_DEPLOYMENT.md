# Deploying the reconstructed app to AWS

These instructions are a deployment plan, not a record of a completed AWS deployment. The original coursework used PHP filenames; this reconstruction uses Flask. No AWS resources were created during preparation. EC2, RDS, public IPv4 addresses, storage and traffic may incur charges. Remove demonstration resources when finished.

## 1. Network and database

Use one region, such as London (`eu-west-2`), and one VPC. Create an EC2 Ubuntu instance in a public subnet, with a private RDS MySQL instance reachable from it. The RDS subnet group needs subnets in at least two Availability Zones, even if you choose a Single-AZ instance for a demo.

- EC2 security group: allow 443 from intended visitors and 80 for HTTPS redirects/certificate setup. Limit SSH 22 to your own IP, or use Systems Manager with its separate permissions.
- RDS security group: allow TCP 3306 **from the EC2 security group**, not from all internet addresses.
- RDS: disable public access, enable storage encryption, set backups appropriate to the demonstration and use a strong database administrator password. Record the endpoint privately.
- Set `require_secure_transport=ON` in the RDS parameter group and use a supported MySQL engine version.

Download the current AWS RDS CA bundle from the official RDS certificate documentation and install it as `/etc/ssl/certs/global-bundle.pem` on EC2. Connect from EC2 with certificate and hostname verification:

```bash
mysql --host=YOUR_RDS_ENDPOINT --user=YOUR_ADMIN --password \
  --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/ssl/certs/global-bundle.pem
```

Create a database and a separate application user. Replace password and username locally; do not commit them:

```sql
CREATE DATABASE events CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'events_app'@'%' IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD' REQUIRE SSL;
GRANT SELECT, INSERT, DELETE ON events.* TO 'events_app'@'%';
```

Run the table definition in `deploy/schema.sql` as the database administrator. The application account does not need CREATE permissions once this has been done. The local `flask --app app init-db` command is also available for an account with migration permissions.

## 2. Private S3 bucket and IAM role

Create a uniquely named bucket in the same region. Keep all S3 Block Public Access settings enabled and enable default server-side encryption. This app stores objects below `events/` and proxies reads through the web server; a public bucket policy and S3 CORS configuration are not required.

Create an IAM role trusted by EC2 using `deploy/ec2-trust-policy.json`. Add the permissions in `deploy/s3-role-policy.json`, replacing the bucket placeholder. Attach the role through an EC2 instance profile. Require IMDSv2 on the instance. Boto3 obtains temporary role credentials automatically; do not put AWS access keys in source code or `.env`.

This policy uses S3-managed encryption (SSE-S3). A customer-managed KMS key would need additional KMS permissions. This project does not configure that variant.

## 3. Prepare Ubuntu and the application

Copy the project to `/opt/aws-events`, excluding local `.env`, caches and data. Then:

```bash
sudo apt update
sudo apt install python3-venv nginx
sudo useradd --system --home /var/lib/aws-events --create-home events
sudo chown -R events:events /opt/aws-events
sudo -u events python3 -m venv /opt/aws-events/.venv
sudo -u events /opt/aws-events/.venv/bin/pip install -r /opt/aws-events/requirements.txt
```

Create `/etc/aws-events.env` manually using `.env.example` as a template. Set ownership to root and permissions to 600. It is loaded by systemd, not served by Flask.

Required values:

```text
APP_ENV=production
SECRET_KEY=<generate a random secret>
ADMIN_PASSWORD_HASH=<generate a password hash>
DATABASE_URL=mysql
STORAGE_BACKEND=s3
DATA_DIR=/var/lib/aws-events
AWS_REGION=eu-west-2
S3_BUCKET=<your bucket>
RDS_HOST=<your endpoint>
RDS_PORT=3306
RDS_DATABASE=events
RDS_USER=events_app
RDS_PASSWORD=<application database password>
RDS_SSL_CA=/etc/ssl/certs/global-bundle.pem
```

Generate the secret and hash in a private terminal, using the virtual environment:

```bash
/opt/aws-events/.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
/opt/aws-events/.venv/bin/python -c 'import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass.getpass("Admin password: ")))'
```

Copy `deploy/events.service` to `/etc/systemd/system/events.service` and run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now events
sudo systemctl status events
curl http://127.0.0.1:8000/health
```

## 4. HTTPS and reverse proxy

Point a domain at EC2 and obtain a valid TLS certificate using your chosen certificate provider. Replace the domain/certificate placeholders in `deploy/nginx.conf`, install it in `/etc/nginx/conf.d/events.conf`, and disable any conflicting default site. The supplied configuration includes a login request limit.

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Use HTTPS for production; secure session cookies will not be sent over plain HTTP. Keep the Gunicorn listener on loopback and do not expose port 8000 in the security group. Configure certificate renewal and test it with your chosen tooling.

## 5. Live checks still to perform

- Load the site over HTTPS and sign in with the administrator password.
- Create an event with a small image; verify the RDS row and private S3 object.
- View the image through the event image route. A direct unauthenticated S3 URL should fail.
- Delete the event; verify both the row and object are removed.
- Verify RDS is unreachable from the internet and rejects connections without TLS.
- Verify the EC2 role has only the intended bucket prefix permissions.
- Verify login throttling, request size limits, service restart and certificate renewal.
- Check logs without recording passwords, cookies or form contents.

## Operational limits

The app is a single-administrator portfolio demo, not a multi-tenant service. Database/storage operations cannot be atomic across MySQL and S3; failures can leave an orphaned record/object, so operational reconciliation is still needed. The `/health` route checks process availability only, not database or S3 health. There is no edit form, pagination, user registration, password reset or timezone conversion. Event dates are interpreted as Europe/London wall time. Add backups, monitoring, migrations, pagination and more extensive integration tests before a larger deployment.

## Official references

- IAM roles and temporary credentials: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html
- RDS MySQL TLS: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.SSLSupport.html
- EC2/RDS connectivity: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/ec2-rds-connect.html
- S3 Block Public Access: https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html
