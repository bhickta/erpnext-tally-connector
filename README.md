# ERPNext-Tally Connector

A reusable base for bidirectional ERPNext ↔ Tally integrations. It provides
flow discovery and dispatch, stable HTTP contracts, shared synchronization
state, a manual ERPNext exporter, and a standalone Windows Control Centre with
manual and automatic synchronization plus pluggable Tally-side execution profiles. Company apps contribute only their selection,
mapping, eligibility, and document-creation policy.

The original Tally-to-ERPNext migration endpoints remain available for masters
(Account, Customer, Supplier, Contact, Address) and transactions (Purchase
Invoice, Sales Invoice, Payment Entry, Journal Entry).

## Prerequisites

- TallyPrime with its HTTP server enabled
- An active ERPNext site
- The India Compliance app when Indian tax fields are synchronized

## Installation

Once you've [set up a Frappe site](https://frappeframework.com/docs/v14/user/en/installation/), installing Express Tally Integration is simple:

1. Download the app using the Bench CLI.

    ```bash
    bench get-app https://github.com/bhickta/erpnext-tally-connector.git
    ```

2. Install the app on your site.

    ```bash
    bench --site [site name] install-app express_tally
    ```


## Configure synchronization

Generate an API key and secret for an ERPNext user with the **Tally Sync User**
role. Run the Windows Control Centre on the Tally computer, enter the connection
and company settings, select the registered flows, and use manual or automatic
sync. No TDL or TCP file is installed in Tally.


## Planned Features

- Sync Alternate (Multiple Units)
- Sync Price Lists

## Bidirectional flow framework

The `express_tally.framework` package provides a versioned API and extension
contract for company apps. Existing migration endpoints remain available and
are not routed through the framework yet.

An installed app can register one or more flows in its `hooks.py`:

```python
tally_integration_flows = [
    "my_company.tally.flows.SalesInvoiceToTally",
    "my_company.tally.flows.TallyReceiptToPaymentEntry",
]
```

ERPNext-to-Tally flows extend `OutboundFlow` and implement `pull` plus
`acknowledge`. Tally-to-ERPNext flows extend `InboundFlow` and implement
`receive`. Each flow owns its business mapping, eligibility rules, and
permissions. The connector owns discovery, validation, batch limits, direction
checks, the HTTP envelope, and reusable `OutboundSyncLog` state handling.

Version 1 endpoints are:

```text
GET  /api/method/express_tally.framework.api.get_flows
GET  /api/method/express_tally.framework.api.pull
POST /api/method/express_tally.framework.api.acknowledge
POST /api/method/express_tally.framework.api.receive
GET  /api/method/express_tally.framework.api.get_status
```

An outbound flow returns documents in a contract understood by its local Tally
agent profile. Payload mapping stays in the contributing app so different
companies can implement different accounting policies without forking the
connector. The bundled `inventory_sales_voucher_v1` profile supports SRV's
current Sales Order/Delivery Note contract.

## Windows Control Centre

End users run a single `ERPNextTallyControlCentre.exe`; Python and Node.js are
not required on the Tally computer. The executable opens a local browser UI for
ERPNext/Tally health, credentials, company and target settings, registered flow
selection, manual sync in either direction, automatic schedules, and recent run
history. Settings and logs are stored under the user's Local AppData directory.

The UI source is the private npm package in `control-centre/`. It has no runtime
dependencies and is built into the Python executable by the Windows release
workflow:

```bash
npm --prefix control-centre ci
npm --prefix control-centre run build
```

An ERPNext-to-Tally flow needs a delivery profile. A Tally-to-ERPNext flow needs
an extraction profile implementing `AgentProfile.collect`; the Control Centre
only enables flows whose matching local profile is installed.

Apps using standard ERPNext selling documents can subclass
`SalesDocumentsToTallyFlow` directly; only the stable key, title, and roles are
required. A custom `SalesDocumentMapper` subclass can override policy without
reimplementing synchronization state or Tally gateway work.

See [Tally flow framework](docs/flow-framework.md) for the extension contract,
agent profiles, state requirements, and an implementation example.

## Contributing

- [Issue Guidelines](https://github.com/frappe/erpnext/wiki/Issue-Guidelines)
- [Pull Request Requirements](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)

## License

[GNU General Public License (v3)]
