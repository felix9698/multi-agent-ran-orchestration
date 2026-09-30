# Third-party notices

The root MIT license applies to the authors' original project code. It does not
replace licenses attached to upstream software, excerpts or derivative patches.

## OpenAirInterface

`oai_patches/*.patch` contains modifications and context from OpenAirInterface
RAN source files. The OAI-format configurations in `configs/` also target that
upstream stack. Copyright in upstream portions remains with their respective
OpenAirInterface Software Alliance / EURECOM and other upstream contributors.
The patch diffs explicitly identify changed lines; the authors' contribution is
the modification, not ownership of surrounding upstream code.

Upstream: <https://gitlab.eurecom.fr/oai/openairinterface5g>  
Official mirror: <https://github.com/OPENAIRINTERFACE/openairinterface5g>

OAI releases have used different license texts. Copies are preserved here:

- [OAI Public License 1.1](LICENSES/OAI-Public-License-1.1.txt), obtained from the
  official mirror's `2025.w30` revision;
- [Collaborative Standards Software License 1.0](LICENSES/CSSL-1.0.txt), obtained
  from the official mirror's `develop` branch on 2026-09-30.

Use the license supplied by the actual upstream revision you build. Including
both texts does not offer a choice of license for upstream code or assert that
the two licenses are interchangeable. The archived patch notes identify
`d8433e8d7fd6b44dc8ab38554caa9bd8eeeb44d7` as the coexistence base, but this
publication has not reconstructed the complete deployed OAI source tree from
that reference. No OAI binary is distributed here.

The unused draft AMF timer patch is not included in this public snapshot.

## External dependencies

OpenAirInterface RAN/CN5G, FlexRIC, UHD, and Python dependencies must be obtained
separately under their own licenses. Header references in `src/xapp/` and
`src/oai/` do not vendor the corresponding upstream libraries. Their licenses
are not replaced by this repository's MIT license.

Project interface profiles describe mappings to O-RAN service models and 3GPP
measurements. They are not a redistribution of the normative standards documents
and do not imply certification by O-RAN ALLIANCE, ETSI or 3GPP.
