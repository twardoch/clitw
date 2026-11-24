---
this_file: src_docs/md/automation.md
---

# Call a provider from Python or an agent

## Look up a provider in Python

`catalog()` returns a dictionary mapping canonical provider names to descriptions. Pass `"openapi"` or `"mcp"` to limit the search. Reading the catalog does not install a launcher or contact a provider.

```python
from clitwy import catalog

services = catalog("openapi")
print(services["mcx-archive-wayback-api"])
```

## Read metadata in automation

`tw search --json` and `tw info --json` both return an object keyed by canonical launcher name, including when a lookup has a single result. For example, use the following code after saving the sample personal overlay. [Personal notes, tags and costs](integrations.md#personal-notes-tags-and-costs)

```python
import json
import subprocess

result = subprocess.run(
    ["tw", "search", "--tag", "monthly-review", "--file", "./envinfo.yaml", "--json"],
    check=True,
    capture_output=True,
    text=True,
)
for name, record in json.loads(result.stdout).items():
    if record.get("price") is not None:
        print(name, record["price"], record.get("currency"), record.get("price_unit"))
        print("Amount source", record.get("sources", {}).get("price"))
```

For an agent answering a cost question, report the amount and unit together. Cite `pricing_url` and `price_as_of` when quoting a public rate. Attribute an amount from the personal file to that user's record. Search results and metadata are available before calling any provider operation.

For a comparison that must use only the package’s public records, read the bundled metadata directly from Python.

```python
from clitwy.service_info import service_catalog

public = service_catalog(include_user=False, include_installed=False)
rate = public["mcx-tavily-apik"]
print(rate["price"], rate["currency"], rate["price_unit"], rate["price_as_of"], rate["pricing_url"])
```

## Run an installed command

After `tw openapi install mcx-archive-wayback-api`, call `run(name, args)` with a list of strings. Set `CLITW_HOME` in the Python process if the provider was installed in a custom state directory.

```python
from clitwy import run

status = run("archive-wayback-api", ["--list", "--json"])
if status:
    raise SystemExit(status)
```

For structured output inside another program, capture stdout from the CLI and decode it after checking the exit status.

```python
import json
import subprocess

result = subprocess.run(
    ["tw", "run", "archive-wayback-api", "--list", "--json"],
    text=True,
    capture_output=True,
    check=True,
    timeout=30,
)
commands = json.loads(result.stdout)
for command in commands:
    print(command["name"])
```

## Give an agent a repeatable workflow

Copy the skill directory you need from the repository into your agent's skill search path. Each directory contains its own instructions.

- Use the provider skill for catalog lookup, installation, schema inspection and checking a result. [Provider skill](https://github.com/twardoch/clitw/blob/main/skills/clitwy/SKILL.md)

For example, ask an agent to find the Internet Archive's closest stored snapshot for a public URL. It can read the operation help, pass the URL as one argument, and return the actual capture timestamp. If the response has no capture, it should report that outcome. For an MCP tool, record whether discovery succeeded and whether the requested operation returned a result. These are distinct observations.
