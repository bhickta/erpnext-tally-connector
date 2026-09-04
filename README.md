# ERPNext-Tally Connector

A reusable base for bidirectional ERPNext ↔ Tally integrations. It provides
flow discovery and dispatch, stable HTTP contracts, shared synchronization
state, a manual ERPNext exporter, and a standalone Windows bridge with pluggable
Tally-side execution profiles. Company apps contribute only their selection,
mapping, eligibility, and document-creation policy.

The original Tally-to-ERPNext migration endpoints remain available for masters
(Account, Customer, Supplier, Contact, Address) and transactions (Purchase
Invoice, Sales Invoice, Payment Entry, Journal Entry).

## Prerequisite
* TDL Files https://github.com/laxmantandon/tally_migration_tdl.git
* Tally Prime
* ERPNext Active Site
* india_compliance app is required https://github.com/resilient-tech/india-compliance

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


## Steps to Configure
* Configure TDL Files in tally
  - follow instruction on this repo https://github.com/laxmantandon/tally_migration_tdl.git

* Generate Authentication Keys
  Create a user with appropriate permission and generate api key and secret
  ![image](https://github.com/laxmantandon/express_tally/assets/24727535/73558d52-d260-4a38-b0a1-8c2ef307a50b)

* Setting up Auth Keys in Tally Prime
  - From Gateway of Tally -> F1 -> Addon Features -> F6
  Set *Enable ERPNext Integration* to Yes and specify auth keys and other parameters
  
  ![image](https://github.com/laxmantandon/express_tally/assets/24727535/5039845f-6a04-49e2-b45c-4a55933630f7)

* Migrating Data from Tally to ERPNext
  - From Gateway of Tally go to Display -> ERPNext -> Migrate to ERPNext
  
  ![image](https://github.com/laxmantandon/express_tally/assets/24727535/d7029c93-1a44-450b-b2f1-ef3655eb28ce)

* Observe result in ERPNext

![image](https://github.com/laxmantandon/express_tally/assets/24727535/f1b46186-89d0-42fb-9136-1df767adbdb7)

## Errors and Exception Handling 
* From Gateway of Tally goto -> ERPNext -> Migration -> Exception (select object type )
  - You can check for exceptions here and make necessary changes in data
  - Check Error Log List in ERPNext for errors
  - Alternatively you check tally event log for more info
    
![image](https://github.com/laxmantandon/express_tally/assets/24727535/726a60b0-7291-4a82-a453-af3eb1d8a2fc)


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
