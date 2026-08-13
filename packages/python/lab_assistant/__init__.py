"""Lab AI Assistant.

A versioned, policy-controlled evidence graph for research labs.

Module dependency direction is enforced by ``tests/contract/test_module_dependencies.py``::

    contracts            -> standard library + pydantic only
    domain               -> contracts
    policy, provenance   -> contracts, domain
    application          -> contracts, domain, policy, provenance
    adapters             -> application ports + external SDKs
    cli, migration       -> application + adapters (composition roots)
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
