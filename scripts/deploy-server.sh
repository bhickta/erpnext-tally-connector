#!/usr/bin/env bash
# Install as root-owned /usr/local/sbin/deploy-express-tally.
set -euo pipefail
if [[ $# != 1 || ! $1 =~ ^deploy\ (dev|production)\ ([0-9a-f]{40})$ ]]; then
    echo 'Expected: deploy dev|production <40-character commit SHA>' >&2
    exit 2
fi
target=${BASH_REMATCH[1]}
revision=${BASH_REMATCH[2]}
case "$target" in
    dev) bench_dir=/home/frappe/dev-bench; site=devrapl; branch=master; group=dev-bench ;;
    production) bench_dir=/home/frappe/prod-bench; site=prodrapl; branch=production; group=prod-bench ;;
esac
exec 9>/var/lock/express-tally-deploy.lock
flock -w 1800 9
# Only the fixed application and these two benches can be deployed by the CI key.
sudo -iu frappe bash -s -- "$bench_dir" "$site" "$branch" "$revision" <<'DEPLOY'
set -euo pipefail
bench_dir=$1; site=$2; branch=$3; revision=$4
export NVM_DIR="$HOME/.nvm"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    set +u
    source "$NVM_DIR/nvm.sh"
    set -u
fi
cd "$bench_dir"
app=apps/express_tally
if [[ ! -d "$app/.git" ]]; then
    [[ ! -e "$app" ]] || { echo 'Existing application is not a Git checkout'; exit 1; }
    git clone https://github.com/bhickta/erpnext-tally-connector.git "$app"
fi
[[ -z $(git -C "$app" status --porcelain) ]] || { echo 'Application has local changes'; exit 1; }
git -C "$app" fetch https://github.com/bhickta/erpnext-tally-connector.git "$branch"
[[ $(git -C "$app" rev-parse FETCH_HEAD) == "$revision" ]] || { echo 'Refusing an outdated deployment'; exit 1; }
echo "Deploying $revision to $site"
bench --site "$site" backup
if git -C "$app" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$app" rev-parse HEAD > "sites/$site/private/express-tally-previous-revision"
fi
git -C "$app" checkout --detach "$revision"
env/bin/pip install -e "$app"
if ! grep -qx express_tally sites/apps.txt; then
    printf '\nexpress_tally\n' >> sites/apps.txt
fi
if ! bench --site "$site" list-apps --format json | env/bin/python -c 'import json,sys; sys.exit(not any("express_tally" in apps for apps in json.load(sys.stdin).values()))'; then
    bench --site "$site" install-app express_tally
fi
bench build --app express_tally
bench --site "$site" migrate
bench --site "$site" clear-cache
env/bin/python -m unittest express_tally.tests.test_framework express_tally.tests.test_sales_voucher_flow
bench --site "$site" execute express_tally.framework.api.get_flows
DEPLOY
supervisorctl restart "$group-web:" "$group-workers:"
supervisorctl status "$group-web:" "$group-workers:"
echo "Successfully deployed $revision to $site"
