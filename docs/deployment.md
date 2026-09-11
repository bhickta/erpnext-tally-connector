# Server deployment

GitHub Actions deploys pushes to `master` to `/home/frappe/dev-bench` (`devrapl`),
and pushes to `production` to `/home/frappe/prod-bench` (`prodrapl`). The Windows
bridge release workflow remains independent. Promote a reviewed revision by
merging master into production and pushing production. The deployment workflow
can also be rerun manually on either branch.

The repository secrets `RAPL_DEPLOY_SSH_KEY` and `RAPL_DEPLOY_KNOWN_HOSTS` contain
a dedicated SSH key and the verified server host key. The authorized key on the
server forces `/usr/local/sbin/deploy-express-tally`, installed from
`scripts/deploy-server.sh` as a root-owned executable. It disallows interactive
shells, port forwarding and arbitrary commands. Updating that script in Git
requires reinstalling it on the server through administrator SSH access.

Deployments are serialized across both environments. The server verifies that
the requested commit is the current branch tip and refuses dirty application
checkouts. Each deployment backs up the site's database and configuration,
installs the application if necessary, builds assets, migrates, clears caches,
runs framework and sales-flow tests, verifies get_flows, and restarts only the
selected bench's web and worker processes. It does not configure Tally clients
or initiate accounting synchronization.

A failed migration or verification fails the workflow. There is no automatic
database rollback. Inspect the Actions log and server state before retrying;
use the site backup and the recorded commit in
`sites/<site>/private/express-tally-previous-revision` for a coordinated manual
rollback if needed. Backups are under the site's `private/backups` directory.
