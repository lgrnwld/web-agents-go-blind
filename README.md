# Where Web Agents Go Blind — reproducibility repository

The current release is in [webagents-ICLR2027-compact](webagents-ICLR2027-compact/README.md). It includes the revised manuscript and all earlier evidence, plus the completed 880-trial instance-variation study.

- [Reproduction instructions](webagents-ICLR2027-compact/README.md): restore the lossless compact archive, then regenerate and verify all 29 statistical tables without model calls.
- [Current paper](webagents-ICLR2027-compact/paper/manuscript.pdf) and [editable source](webagents-ICLR2027-compact/paper/main.tex).
- [Instance-variation results](webagents-ICLR2027-compact/analysis/instance-variation-20260919/report.md) and [protocol](webagents-ICLR2027-compact/analysis/instance-variation-20260919/PROTOCOL.md).
- [Revision notes](webagents-ICLR2027-compact/TEXT-CHANGES.md) and [validated restoration/reanalysis](webagents-ICLR2027-compact/COMPACT-VALIDATION.json).

The release contains 5,960 study outcomes plus 48 control outcomes. The added study uses 20 task-instance blocks paired across four deployments and eleven conditions. Original and new results remain separate. The original illustrated pipeline is preserved.

Follow the instructions inside the release directory; do not commit expanded `restored/`, generated `reproduced/`, environment files, or local credentials. Fresh inference is optional and distinct from reproducing the archived analysis.
