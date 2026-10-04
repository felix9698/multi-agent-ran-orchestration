# Deployment configuration

[liveconsole-profile.example.json](liveconsole-profile.example.json) demonstrates
the profile layout without publishing an author's endpoints or credentials.
Loading it can configure the GUI, but it intentionally cannot start Live: its
referenced `local/` deployment files do not exist in this repository.

Create your own local copy, then provide the actual integration values,
capability manifest and assurance binding for the prepared RAN. Preserve their
cross-document digest relationships. Their formats are defined by
[`tools/liveconsole/profile.py`](../tools/liveconsole/profile.py) and
[`assurance/contracts/live_binding.py`](../assurance/contracts/live_binding.py).
Keep credential values outside this repository and use references in deployment
documents. `deployment/local/` and `*.local.json` are ignored by Git.

The generic `/opt/ran-lab/` paths and `ran-node*`/`ran-ue*` SSH names in retained
preparation helpers describe an **example** layout; they are not installed by
cloning the repository. Radio calibration, host keys, binary/configuration hashes
and subscriptions must correspond to your own deployment. Use a labctl
inventory that describes your testbed; the shipped profile is an example.
