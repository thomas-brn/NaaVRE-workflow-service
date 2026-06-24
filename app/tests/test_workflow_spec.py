"""Unit tests for the Argo workflow spec template.

These render ``argo_workflowSpec.j2`` in isolation (no Argo, no cluster, no
auth) and assert on the generated YAML. They guard the spec-level ``volumes``
declaration, which must be present for every submission so that pods can mount
the workspace emptyDir and the user-storage PVCs.
"""

import yaml
from jinja2 import Environment, PackageLoader, StrictUndefined


def _render(cron_schedule):
    """Render the workflow spec head without any nodes.

    Passing ``nodes={}`` skips the per-node body (and its ``include`` calls),
    so we can exercise the spec-level ``volumes`` block on its own.
    """
    env = Environment(
        loader=PackageLoader('app', 'templates'),
        undefined=StrictUndefined,
    )
    template = env.get_template('argo_workflowSpec.j2')
    return template.render(
        workflow_name='wf',
        workflow_service_account='argo-executor',
        nodes={},
        dependencies_dag={},
        naavrewf2_payload_params=[],
        k8s_secret_name=None,
        workdir_storage_size='1Gi',
        default_max_branches='32',
        extraEnv=[],
        vlab_slug='openlab',
        cron_schedule=cron_schedule,
        extraVolumeMounts=[
            {'name': 'naa-vre-public', 'mountPath': '/x'},
            {'name': 'naa-vre-user-data', 'mountPath': '/y',
             'subPath': '{unescaped_username}'},
        ],
    )


def _volume_names(rendered):
    spec = yaml.safe_load(rendered.replace('{unescaped_username}', 'someuser'))
    return [v['name'] for v in (spec.get('volumes') or [])]


def test_volumes_declared_for_normal_submission():
    # Regression: the volumes block used to be gated behind cron_schedule, so a
    # normal (non-cron) submission declared no volumes while pods still
    # referenced them -> Argo rejected the workflow.
    names = _volume_names(_render(cron_schedule=None))
    assert 'workspace' in names
    assert 'naa-vre-user-data' in names
    assert 'naa-vre-public' in names


def test_volumes_declared_for_cron_submission():
    names = _volume_names(_render(cron_schedule='0 0 * * *'))
    assert 'workspace' in names
    assert 'naa-vre-user-data' in names


def test_workspace_volume_declared_once():
    # workspace lives outside the extraVolumeMounts loop, so it must not be
    # duplicated per mount (duplicate volume names are invalid in Argo).
    for cron in (None, '0 0 * * *'):
        names = _volume_names(_render(cron_schedule=cron))
        assert names.count('workspace') == 1
