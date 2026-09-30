"""O-RAN side of the Agentic Intent Coordinator.
Subpackages own one responsibility each, and they meet only at the interfaces
the oran-aic/1.0.0 contract fixes:
    contract/     vendored contract bundle, JCS, digest gate, validator, errors
    rapp/         the rApp itself: intent -> A1 policy, R1 consumer, S3/S4
    nonrt/        minimal Non-RT RIC Framework: R1 service, A1-P Consumer
    o1/           O1 Performance Assurance consumer, PM normalisation
    mocks/        contract-faithful stand-ins for the other researcher's half
    conformance/  the black-box scenario runner that judges all of the above
"""
