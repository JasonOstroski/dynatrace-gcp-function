import asyncio
import re
from typing import NamedTuple, List, Optional
from urllib.parse import urljoin

from aiohttp import ClientSession

from lib.context import LoggingContext
from lib.credentials import get_all_accessible_projects, fetch_dynatrace_url, fetch_dynatrace_api_key

service_name_pattern = re.compile(r"^projects\/([\w,-]*)\/services\/([\w,-.]*)$")

GCP_SERVICE_USAGE_URL = 'https://serviceusage.googleapis.com/v1/projects/'

required_services = [
    'monitoring.googleapis.com',
    'cloudresourcemanager.googleapis.com'
]

dynatrace_required_token_scopes = [
    'metrics.ingest',
]

FastCheckResult = NamedTuple('FastCheckResult', [('projects', List[str])])


def find_service_name(service):
    return service_name_pattern.match(service).group(2) if service_name_pattern.match(service) else None


def valid_dynatrace_scopes(token_metadata: dict):
    """Check whether Dynatrace token metadata has required scopes to start ingest metrics"""
    token_scopes = token_metadata.get('scopes', [])
    return all(scope in token_scopes for scope in dynatrace_required_token_scopes) if token_scopes else False



async def get_dynatrace_token_metadata(session: ClientSession, context: LoggingContext, dynatrace_url: str, dynatrace_api_key: str, timeout: Optional[int] = 2) -> dict:
    try:
        response = await session.post(
            url=f"{dynatrace_url.rstrip('/')}/api/v1/tokens/lookup",
            headers={
                "Authorization": f"Api-Token {dynatrace_api_key}",
                "Content-Type": "application/json; charset=utf-8"
            },
            json={
                "token": dynatrace_api_key
            },
            timeout=timeout)
        if response.status != 200:
            context.log(f'Unable to get Dynatrace token metadata: {response.status}, url: {response.url}, reason: {response.reason}')
            return {}

        return await response.json()
    except Exception as e:
        context.log(f'Unable to get Dynatrace token metadata. Error details: {e}')
        return {}


class FastCheck:

    def __init__(self, session: ClientSession, token: str, logging_context: LoggingContext):
        self.session = session
        self.logging_context = logging_context
        self.token = token

    async def list_services(self, project_id: str, timeout: Optional[int] = 2):
        try:
            response = await self.session.get(
                urljoin(GCP_SERVICE_USAGE_URL, f'{project_id}/services'),
                headers={
                    "Authorization": f'Bearer {self.token}'
                },
                params={
                    "filter": "state:ENABLED"
                },
                timeout=timeout)
            if response.status != 200:
                self.logging_context.log(f'Http error: {response.status}, url: {response.url}, reason: {response.reason}')
                return {}

            return await response.json()
        except Exception as e:
            self.logging_context.log(f'Unable to get project: {project_id} services list. Error details: reason is {type(e).__name__} {e}')
            return {}

    async def _check_services(self, project_id):
        list_services_result = await self.list_services(project_id)
        service_names = [find_service_name(service['name']) for service in list_services_result.get('services', [])]
        if not all(name in service_names for name in required_services):
            self.logging_context.log(f'Cannot monitor project: \'{project_id}\'. '
                                     f'Enable required services: {required_services}')
            return None
        return service_names

    async def _check_dynatrace(self, project_id):
        try:
            dynatrace_url = await fetch_dynatrace_url(self.session, project_id, self.token)
            dynatrace_access_key = await fetch_dynatrace_api_key(self.session, project_id, self.token)
            if not dynatrace_url or not dynatrace_access_key:
                self.logging_context.log(f'No Dynatrace secrets: DYNATRACE_URL, DYNATRACE_ACCESS_KEY for project: {project_id}.'
                                         f'Add required secrets to Secret Manager.')
                return None

            token_metadata = await get_dynatrace_token_metadata(self.session, self.logging_context, dynatrace_url, dynatrace_access_key)
            if token_metadata.get('revoked', None) or not valid_dynatrace_scopes(token_metadata):
                self.logging_context.log(f'Dynatrace API Token for project: \'{project_id}\'is not valid. '
                                         f'Check expiration time and required token scopes: {dynatrace_required_token_scopes}')
                return None
        except Exception as e:
            self.logging_context.log(f'Unable to get Dynatrace Secrets for project: {project_id}. Error details: {e}')
            return None

        return dynatrace_url, token_metadata

    async def _init_(self) -> FastCheckResult:
        project_list = await get_all_accessible_projects(self.logging_context, self.session, self.token)

        ready_to_monitor = []
        for project_id in project_list:
            tasks = [self._check_services(project_id), self._check_dynatrace(project_id)]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            if all(result is not None for result in results):
                ready_to_monitor.append(project_id)

        return FastCheckResult(projects=ready_to_monitor)

    def __await__(self):
        return self._init_().__await__()
