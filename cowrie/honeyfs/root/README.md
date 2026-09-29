# Bait files

These are intended as decoy content for the emulated filesystem.

**Important Cowrie mechanic:** Cowrie does not automatically list files in `ls` just
because they exist in `honeyfs`. Directory trees are loaded from `fs.pickle`. Files in
`honeyfs` only provide content when an attacker opens (`cat`) a path that already exists
in the pickle. To make brand-new paths appear in `ls`, they must be added to the pickle
itself via Cowrie's `fsctl` tool.

**Use fake values only, never real ones.** Everything here must be worthless: no real
keys, no working credentials, no actual wallet phrases. Assume any file in this directory
is public the moment it is deployed.
