#!/usr/bin/env bash
# Invite a teammate to the Arena dashboard (Cognito). Prints a one-time temporary password;
# the user must set a new password at first login. Usage: infra/arena/add-user.sh name@example.com
set -euo pipefail
email="${1:?usage: add-user.sh email}"
dir="$(cd "$(dirname "$0")" && pwd)"
pool="$(terraform -chdir="$dir" output -raw cognito_user_pool_id)"
region="$(echo "$pool" | cut -d_ -f1)"
temp="$(python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print('Ar'+''.join(secrets.choice(a) for _ in range(13))+'7x')")"
aws cognito-idp admin-create-user --region "$region" --user-pool-id "$pool" --username "$email" \
  --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
  --temporary-password "$temp" --message-action SUPPRESS >/dev/null
echo "user:      $email"
echo "temporary: $temp   (share privately; must be changed at first login, valid 7 days)"
echo "dashboard: $(terraform -chdir="$dir" output -raw dashboard_url)"
