import base64
import json
import os
from abc import ABC

import jinja2
import requests
import yaml
from slugify import slugify

from app.models.naavrewf2_payload import Naavrewf2Payload
from app.models.vl_config import VLConfig
from app.services.wf_engines.wf_engine import WFEngine


def is_cron(workflow_dict):
    if workflow_dict['kind'] == 'CronWorkflow':
        return True
    return False


def include_file(env):
    def _include(name, **kwargs):
        template = env.get_template(name)
        return template.render(**kwargs)

    return _include


class ArgoEngine(WFEngine, ABC):
    workflow_template: jinja2.Template
    api_endpoint: str
    token: str

    def __init__(self, vl_config: VLConfig):
        super().__init__(vl_config)
        self.template_env.globals['include'] = include_file(self.template_env)
        self.workflow_template = self.template_env.get_template(
            'argo_workflow_top.j2')
        # Add '/' at the end of the endpoint if not present
        if vl_config.wf_engine_config.api_endpoint[-1] != '/':
            vl_config.wf_engine_config.api_endpoint += '/'
        self.api_endpoint = (vl_config.wf_engine_config.api_endpoint +
                             "api/v1/workflows/" +
                             vl_config.wf_engine_config.namespace)
        self.api_cron_endpoint = (vl_config.wf_engine_config.api_endpoint +
                                  "api/v1/cron-workflows/" +
                                  vl_config.wf_engine_config.namespace)
        self.token = (vl_config.wf_engine_config.access_token.replace
                      ('"', '')).replace('Bearer ', '')
        self.extraVolumeMounts = vl_config.wf_engine_config.extraVolumeMounts
        # Per-virtual-lab extra env vars injected into every cell container
        # (see wf_engine_config.py); used e.g. to configure cloud storage
        # access for the fdo-writer special cell without exposing it as a
        # user-facing secret in the composer.
        self.extraEnv = vl_config.wf_engine_config.extraEnv
        self.secrets_creator_api_endpoint = (vl_config.wf_engine_config.
                                             secrets_creator_api_endpoint)
        if not self.secrets_creator_api_endpoint:
            raise Exception("secrets_creator_api_endpoint is not set in the "
                            "configuration")
        # Make sure that the secrets_creator_api_endpoint has a '/' at the end
        if not self.secrets_creator_api_endpoint.endswith('/'):
            self.secrets_creator_api_endpoint += '/'
        self.secrets_creator_api_token = (vl_config.wf_engine_config.
                                          secrets_creator_api_token)
        if not self.secrets_creator_api_token:
            raise Exception("secrets_creator_api_token is not set in the "
                            "configuration")

    @property
    def user_extraVolumeMounts(self):
        """ Returns extraVolumeMounts for the current user

        Filters extraVolumeMounts:
            - if the volumeMount has no allowedGroups, include it
            - else:
                - if the user is not a member of any groups of allowedGroups,
                  drop the volumeMount
                - else, include the volumeMount, dropping the allowedGroup
                  entry to return a k8s-compatible object.
        """
        if self.extraVolumeMounts:
            return [
                {k: v for k, v in volume_mount.items() if k != 'allowedGroups'}
                for volume_mount in self.extraVolumeMounts
                if (
                        ('allowedGroups' not in volume_mount)
                        or (
                                set(self.user_groups)
                                & set(volume_mount['allowedGroups'])
                        )
                )
                ]
        else:
            return None

    def submit(self):
        workflow_dict = self.naavrewf2_2_argo_workflow(create_secrets=True)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        if is_cron(workflow_dict):
            api_endpoint = self.api_cron_endpoint
            workflow_type = 'cronWorkflow'
            run_url_resource = 'cron-workflows'
        else:
            api_endpoint = self.api_endpoint
            workflow_type = 'workflow'
            run_url_resource = 'workflows'
        response = requests.post(api_endpoint,
                                 json={workflow_type: workflow_dict},
                                 headers=headers,
                                 verify=os.getenv('VERIFY_SSL', 'true').
                                 lower() == 'true')

        if response.status_code != 200:
            raise Exception('Error submitting workflow: ' + response.text)
        workflow_name = response.json()["metadata"]["name"]

        run_url = (self.vl_config.wf_engine_config.api_endpoint +
                   run_url_resource + "/" +
                   f"{self.vl_config.wf_engine_config.namespace}/"
                   f"{workflow_name}")
        return {'run_url': run_url,
                'naavrewf2': self.naavrewf2_payload.naavrewf2}

    def naavrewf2_2_argo_workflow(self, create_secrets: bool = True):
        if self.secrets and create_secrets:
            k8s_secret_name = self.add_secrets_to_k8s()
        else:
            k8s_secret_name = None

        workflow_name = 'n-a-a-vre-' + slugify(self.user_name)
        service_account = self.vl_config.wf_engine_config.service_account
        workdir_storage_size = (self.vl_config.
                                wf_engine_config.workdir_storage_size)
        default_max_branches = (
                    self.vl_config.wf_engine_config.default_max_branches
                    or 100)
        workflow_yaml = self.workflow_template.render(
            vlab_slug=self.virtual_lab_name,
            dependencies_dag=self.parser.get_dependencies_dag(),
            nodes=self.nodes,
            naavrewf2_payload_params=self.naavrewf2_payload_params or [],
            k8s_secret_name=k8s_secret_name,
            workflow_name=workflow_name,
            workflow_service_account=service_account,
            workdir_storage_size=workdir_storage_size,
            cron_schedule=self.cron_schedule,
            extraVolumeMounts=self.user_extraVolumeMounts or [],
            extraEnv=self.extraEnv or [],
            default_max_branches=default_max_branches
        )

        workflow_dict = yaml.safe_load(
            workflow_yaml.replace('{unescaped_username}', self.user_name))

        if os.getenv('DEBUG') == 'true':
            print("Generated Argo Workflow YAML:")
            # Save to /tmp/ for inspection
            with open(f"/tmp/{workflow_name}_workflow.yaml", "w") as f:
                f.write(workflow_yaml)
            print(yaml.dump(workflow_dict, sort_keys=False))
        return workflow_dict

    def get_wf(self, workflow_url: str):
        # Get the workflow from the Argo API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        workflow_status_url = self.get_workflow_status_url(
            workflow_url=workflow_url)
        response = requests.get(workflow_status_url, headers=headers,
                                verify=os.getenv('VERIFY_SSL', 'true').
                                lower() == 'true')
        if response.status_code != 200:
            raise Exception('Error getting workflow: ' + response.text)
        return response.json()

    def delete(self, workflow_url: str):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        workflow_status_url = self.get_workflow_status_url(
            workflow_url=workflow_url)
        response = requests.delete(workflow_status_url, headers=headers,
                                   verify=os.getenv('VERIFY_SSL', 'true').
                                   lower() == 'true')
        if response.status_code != 200:
            raise Exception('Error getting workflow status: ' + response.text)
        return response.json()

    def get_workflow_status_url(self, workflow_url: str):
        """
        Extracts the workflow status URL from the provided workflow URL.
        """
        if 'cron-workflows' in workflow_url:
            api_endpoint = self.api_cron_endpoint
        else:
            api_endpoint = self.api_endpoint
        workflow_name = workflow_url.split('/')[-1]
        # If the endpoint does not have a '/' at the end, add it
        if not api_endpoint.endswith('/'):
            api_endpoint += '/'
        return api_endpoint + workflow_name

    def get_wfs_for_recurring_wf(self, workflow_url: str):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        label_selector = ("label=workflows.argoproj.io%2Fcron-workflow%3D" +
                          workflow_url.split('/')[-1])
        api_endpoint = self.api_endpoint + "?" + label_selector
        workflows_response = requests.get(api_endpoint, headers=headers,
                                          verify=os.getenv('VERIFY_SSL',
                                                           'true').lower() ==
                                          'true')
        if workflows_response.status_code != 200:
            raise Exception('Error getting workflows for recurring workflow: '
                            + workflows_response.text)
        return workflows_response.json().get('items', [])

    def add_secrets_to_k8s(self):
        body = {}
        # Assumes secures are a list of
        # [{name:secret_name,value: secret_value}]
        for secret in self.secrets:
            secret_name = secret['name']
            secret_value = secret['value']
            body[secret_name] = base64.b64encode(
                secret_value.encode()).decode()

        resp = requests.post(
            f"{self.secrets_creator_api_endpoint}",
            verify=os.getenv('VERIFY_SSL', 'true').lower() == 'true',
            headers={
                'accept': 'application/json',
                'X-Auth': self.secrets_creator_api_token,
                'Content-Type': 'application/json'
            },
            data=json.dumps(body),
        )
        resp.raise_for_status()
        secret_name = resp.json()['secretName']
        return secret_name

    def lint(self, workflow_payload: Naavrewf2Payload):
        # For now, we will just check that the workflow can be rendered
        try:
            self.naavrewf2_2_argo_workflow(create_secrets=False)
        except jinja2.exceptions.TemplateError as e:
            wf_nodes = workflow_payload.naavrewf2.nodes
            wf_links = workflow_payload.naavrewf2.links
            # Find the nodes that are not connected to any link
            connected_nodes = []
            for node_id in wf_nodes:
                for link_id in wf_links:
                    link = wf_links[link_id]
                    from_node_id = link.from_.nodeId
                    to_node_id = link.to.nodeId
                    if node_id == from_node_id or node_id == to_node_id:
                        connected_nodes.append(node_id)
                        break

            # Find the nodes that are not connected to any link
            unconnected_nodes = [node_id for node_id in wf_nodes
                                 if node_id not in connected_nodes]
            # Get the names of the unconnected nodes
            unconnected_node_names = [wf_nodes[node_id].properties.cell.title
                                      for node_id in unconnected_nodes]
            raise Exception(f"Error rendering workflow template: {str(e)}. "
                            f""f"Unconnected nodes: {unconnected_node_names}")
