"""The `--schema` fixtures `saddle yaml-check --schema` is judged on.

Alongside tests/yaml_check_fixtures.py, which holds the files the parse,
duplicate-key and unsafe-tag rules are judged on: those fixtures are read as
they are written there, because a schema takes nothing away from them. Here
every file is a deployment bundle, every refusal a way it fails the shape, and
one fixture pairs a shape refusal with a key refusal in one document.

`SCHEMAS` is every stand-in schema file a test hands to `--schema`; the shape
every other fixture is written against is the committed one, `BUNDLE_SCHEMA`.
`SCHEMA_GOOD` is the text of every file that shape must take and `SCHEMA_BAD`
the text of every file it must refuse, with one `Refusal` per shape violation
the report has to name: the 1-based line and column in the whole file where the
offending node starts, and a needle the message must contain -- the document
path of that node, keys joined by dots and sequence items by an index, then what
the schema says is wrong with it. Line numbers count from the start of the whole
file, which is why the second document of a fixture does not start at 1.

`BUNDLE_SCHEMA` is resolved from this file rather than the working directory,
which every test starts empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from yaml_check_fixtures import Refusal

# The bundle shape, exported once from a deployment tool's published JSON Schema
# and committed here: `--schema` reads this file and no other.
BUNDLE_SCHEMA: Final[Path] = (
    Path(__file__).resolve().parent / "fixtures" / "deployment-bundle.schema.json"
)

# Stand-in schemas, for the tests that are about *which* schema a run was given
# rather than about the bundle shape: one that says nothing, one that names only
# `apiVersion`, and one that names `apiVersion` and allows nothing else.
SCHEMAS: Final[dict[str, str]] = {
    "any": "true",
    "api-version-only": (
        '{"type": "object", "required": ["apiVersion"],'
        ' "properties": {"apiVersion": {"type": "string"}}}'
    ),
    "api-version-and-nothing-else": (
        '{"type": "object", "required": ["apiVersion"],'
        ' "properties": {"apiVersion": {"type": "string"}},'
        ' "additionalProperties": false}'
    ),
}

SCHEMA_GOOD: Final[dict[str, str]] = {
    # The bundle as the tool writes it: every required key present, every value
    # of the type its schema says, a block scalar holding a command of two lines,
    # a task that adds a timeout, a second job, and volumes with sizes.
    "bundle-as-the-tool-writes-it": """apiVersion: bundle/v1
presets:
  nightly:
    when: [nightly]
resources:
  jobs:
    nightly:
      when: [nightly]
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
          timeout: 30
        - name: report
          run: |
            ./report --full
            ./report --mail
    pages:
      tasks:
        - name: build
          run: npm run build
  volumes:
    - name: cache
      size: 512
""",
    # Merge keys applied before the shape is checked: `when` arrives with the
    # preset the job merges, so the job that never writes one is a job the shape
    # takes, and the value the merge brings in is one the schema accepts.
    "a-job-that-merges-a-preset": """apiVersion: bundle/v1
presets:
  nightly-tasks: &nightly-tasks
    tasks:
      - name: build
        run: npm run build
resources:
  jobs:
    nightly:
      <<: *nightly-tasks
    pages:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
""",
    # A merge the job overrides rather than only receives: the preset brings `when`
    # and `tasks`, the job writes `when` again with its own value, and the document
    # the shape is run on holds the job's `when` with the preset's tasks. Which side
    # a merge wins on is the loader's decision, and the shape only ever sees its
    # result.
    "a-job-that-writes-again-a-key-its-preset-brings": """apiVersion: bundle/v1
presets:
  nightly: &nightly
    when: [nightly]
    tasks:
      - name: build
        run: npm run build
resources:
  jobs:
    nightly:
      <<: *nightly
      when: [on-push]
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
          timeout: 5
""",
    # The optional key at each bound the shape puts on it: `timeout` at its minimum,
    # `when` empty, and a volume whose size is the smallest the shape allows.
    "optional-keys-at-their-bounds": """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      when: []
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
          timeout: 0
  volumes:
    - name: cache
      size: 1
""",
}

SCHEMA_BAD: Final[dict[str, tuple[str, list[Refusal]]]] = {
    # The misspelt key, which is the defect this option exists to catch before a
    # deployment: the shape has `name`, the file writes `nmae`. Every key the
    # mapping holds that the shape does not allow is named, at the mapping.
    "misspelt-key": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      tasks:
        - nmae: fetch
          run: curl -sf https://example.invalid/nightly
""",
        [
            Refusal(6, 11, "resources.jobs.nightly.tasks[0]: 'name' is a required property"),
            Refusal(
                6,
                11,
                "resources.jobs.nightly.tasks[0]: Additional properties are not allowed"
                " ('nmae' was unexpected)",
            ),
        ],
    ),
    # The missing required key, in the second item of a sequence and nowhere
    # else: reported on the item that lacks it, at the `-` that item starts on,
    # with the index of that item in the path.
    "missing-required-key-in-a-sequence-item": (
        """---
# A deployment bundle whose nightly job has a task with no name.
apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
        - run: curl -sf https://example.invalid/report
""",
        [Refusal(10, 11, "resources.jobs.nightly.tasks[1]: 'name' is a required property")],
    ),
    # A violation deep in a document, reached only by walking out of the
    # document, through three mappings and into a sequence to a nested key, and
    # a second one on the next line of the same item: two problems, at the lines
    # they are written on and in column order on the one line. The path names
    # every step of that walk.
    "violation-deep-in-a-document": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
        - name: report
          timeout: -1
          run:
""",
        [
            Refusal(
                9,
                20,
                "resources.jobs.nightly.tasks[1].timeout: -1 is less than the minimum of 0",
            ),
            Refusal(
                10,
                15,
                "resources.jobs.nightly.tasks[1].run: None is not of type 'string'",
            ),
        ],
    ),
    # Every document of a multi-document file fails the shape, twice in the file
    # and in different ways each time: an empty mapping a required key is named
    # in, an `apiVersion` the enumeration does not name, and a task that writes
    # no `run`. No document is skipped because an earlier one already failed, and
    # the positions count from the start of the whole file, not of the document.
    "violation-in-each-document-of-a-file": (
        """apiVersion: bundle/v1
resources: {}
---
apiVersion: bundle/v2
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
---
apiVersion: bundle/v1
resources: {}
---
apiVersion: bundle/v2
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
""",
        [
            Refusal(2, 12, "resources: 'jobs' is a required property"),
            Refusal(4, 13, "apiVersion: 'bundle/v2' is not one of ['bundle/v1']"),
            Refusal(9, 11, "resources.jobs.nightly.tasks[0]: 'run' is a required property"),
            Refusal(12, 12, "resources: 'jobs' is a required property"),
            Refusal(14, 13, "apiVersion: 'bundle/v2' is not one of ['bundle/v1']"),
            Refusal(19, 11, "resources.jobs.nightly.tasks[0]: 'run' is a required property"),
        ],
    ),
    # Carried in by a merge: the bad value is written once, in the preset, and a
    # task reaches it with `<<`. The document that never writes it fails the
    # shape, and the problem is at the line the value is written on, where the
    # alias resolves to, not where the merge is written.
    "violation-carried-in-by-a-merge": (
        """apiVersion: bundle/v1
presets:
  task-defaults: &task-defaults
    timeout: -5
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
          <<: *task-defaults
""",
        [
            Refusal(
                4,
                14,
                "resources.jobs.nightly.tasks[0].timeout: -5 is less than the minimum of 0",
            ),
        ],
    ),
    # A document that is not a mapping at all fails the shape of the document
    # itself: the path is empty, so the problem names no key in front of it, and
    # it is at the line the document starts on.
    "a-document-that-is-a-list": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
---
- name: fetch
  run: curl -sf https://example.invalid/nightly
""",
        [
            Refusal(
                9,
                1,
                "[{'name': 'fetch', 'run': 'curl -sf https://example.invalid/nightly'}] "
                "is not of type 'object'",
            )
        ],
    ),
    # A key the document itself needs, never written: the problem is the
    # document's own, at the first line of the file.
    "missing-a-key-the-document-itself-needs": (
        """apiVersion: bundle/v1
""",
        [Refusal(1, 1, "'resources' is a required property")],
    ),
    # The key rule and the shape rule on one document, which is one problem each,
    # and in the order the opposite of the one the checks reach them in: the shape
    # refuses the `apiVersion` at the top of the file, and the key rule refuses the
    # `nightly` written twice further down. A file needs both rules at once -- one
    # rule reports neither of the other's problems -- and the report is in the order
    # the file writes them, not the order the checks found them in.
    "the-key-rule-and-the-shape-rule-on-one-document": (
        """apiVersion: bundle/v2
resources:
  jobs:
    nightly:
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
    nightly:
      tasks:
        - name: report
          run: curl -sf https://example.invalid/report
""",
        [
            Refusal(1, 13, "apiVersion: 'bundle/v2' is not one of ['bundle/v1']"),
            Refusal(
                8,
                5,
                "duplicate key 'nightly': it is written twice in this mapping, the first at line 4",
            ),
        ],
    ),
    # A key the shape does not allow at the top of the document, where the only
    # thing allowed is what the document itself is held to: `resource` is not
    # `resources`, and the problem about it is a problem about the whole document.
    "a-key-the-document-itself-does-not-allow": (
        """apiVersion: bundle/v1
resource: cache
resources:
  jobs:
    nightly:
      tasks:
        - name: build
          run: npm run build
""",
        [Refusal(1, 1, "Additional properties are not allowed ('resource' was unexpected)")],
    ),
    # The same rule one level down, where the problem has a document path to name it
    # by: `jobz` stands in for `jobs`, and the report says which mapping wrote it.
    "a-key-a-mapping-one-level-down-does-not-allow": (
        """apiVersion: bundle/v1
resources:
  jobz:
    nightly: []
  jobs:
    nightly:
      tasks:
        - name: build
          run: npm run build
""",
        [Refusal(3, 3, "resources: Additional properties are not allowed ('jobz' was unexpected)")],
    ),
    # Every bound the shape puts on a task, each crossed by one entry while the
    # entries beside it stay inside: a `when` that is a word and not the list of
    # words it is, a timeout below the minimum, and a `run` written as no value.
    "every-bound-on-a-task-crossed-at-once": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      when: nightly
      tasks:
        - name: fetch
          run: curl -sf https://example.invalid/nightly
          timeout: 30
        - name: report
          run: curl -sf https://example.invalid/report
          timeout: -1
        - name: send
          run:
""",
        [
            Refusal(5, 13, "resources.jobs.nightly.when: 'nightly' is not of type 'array'"),
            Refusal(
                12,
                20,
                "resources.jobs.nightly.tasks[1].timeout: -1 is less than the minimum of 0",
            ),
            Refusal(
                14,
                15,
                "resources.jobs.nightly.tasks[2].run: None is not of type 'string'",
            ),
        ],
    ),
    # A job that declares its task list and fills it with nothing: the key is there,
    # and the shape says a job is a job only if it has something to run.
    "a-job-with-an-empty-task-list": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      when: [nightly]
      tasks: []
""",
        [Refusal(6, 14, "resources.jobs.nightly.tasks: [] should be non-empty")],
    ),
    # The same rule on the mapping of jobs itself: a bundle that names `jobs` and
    # lists none.
    "a-bundle-that-declares-no-jobs": (
        """apiVersion: bundle/v1
resources:
  jobs: {}
""",
        [Refusal(3, 9, "resources.jobs: {} should be non-empty")],
    ),
    # A misspelt key where a job's own keys are: `taskz` is not `tasks`, so the job
    # is refused twice over, once for the key it does not have and once for the one
    # it wrote instead.
    "a-key-a-job-does-not-allow": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      taskz:
        - name: build
          run: npm run build
""",
        [
            Refusal(5, 7, "resources.jobs.nightly: 'tasks' is a required property"),
            Refusal(
                5,
                7,
                "resources.jobs.nightly: Additional properties are not allowed "
                "('taskz' was unexpected)",
            ),
        ],
    ),
    # A misspelt key inside a sequence item that is not a task: volumes are held to
    # their own shape, and the path says which item of which list wrote it.
    "a-key-a-volume-does-not-allow": (
        """apiVersion: bundle/v1
resources:
  jobs:
    nightly:
      tasks:
        - name: build
          run: npm run build
  volumes:
    - name: cache
      sie: 512
      size: 512
""",
        [
            Refusal(
                9,
                7,
                "resources.volumes[0]: Additional properties are not allowed "
                "('sie' was unexpected)",
            )
        ],
    ),
}

# Schema files that are not a schema. `True` makes a validator that accepts
# every document, so a `--schema` that cannot be used, and one that asks for no
# shape at all, are both named as problems: a run that cannot hold its files to
# the shape it was asked for is not a run that found nothing wrong.
BAD_SCHEMAS: Final[dict[str, str]] = {
    "schema-that-is-not-json": "{ not JSON at all",
    "json-that-is-not-a-schema": '{"type": 5}',
}
