import os
import random
import string
import time
from datetime import datetime, timedelta, timezone
from typing import List

import httpx
import pytest
from anthropic.types.beta import BetaMessage
from anthropic.types.beta.messages import BetaMessageBatchIndividualResponse, BetaMessageBatchSucceededResult
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall as OpenAIToolCall
from openai.types.chat.chat_completion_message_tool_call import Function as OpenAIFunction
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from letta.config import LettaConfig
from letta.constants import (
    BASE_MEMORY_TOOLS,
    BASE_SLEEPTIME_TOOLS,
    BASE_TOOLS,
    LETTA_TOOL_EXECUTION_DIR,
    MCP_TOOL_TAG_NAME_PREFIX,
    MULTI_AGENT_TOOLS,
)
from letta.embeddings import embedding_model
from letta.functions.functions import derive_openai_json_schema, parse_source_code
from letta.functions.mcp_client.types import MCPTool
from letta.helpers import ToolRulesSolver
from letta.jobs.types import ItemUpdateInfo, RequestStatusUpdateInfo, StepStatusUpdateInfo
from letta.orm import Base, Block
from letta.orm.block_history import BlockHistory
from letta.orm.enums import ActorType, JobType, ToolType
from letta.orm.errors import NoResultFound, UniqueConstraintViolationError
from letta.schemas.agent import AgentStepState, CreateAgent, UpdateAgent
from letta.schemas.block import Block as PydanticBlock
from letta.schemas.block import BlockUpdate, CreateBlock
from letta.schemas.embedding_config import EmbeddingConfig
from letta.schemas.enums import AgentStepStatus, JobStatus, MessageRole, ProviderType
from letta.schemas.environment_variables import SandboxEnvironmentVariableCreate, SandboxEnvironmentVariableUpdate
from letta.schemas.file import FileMetadata as PydanticFileMetadata
from letta.schemas.identity import IdentityCreate, IdentityProperty, IdentityPropertyType, IdentityType, IdentityUpdate, IdentityUpsert
from letta.schemas.job import BatchJob
from letta.schemas.job import Job
from letta.schemas.job import Job as PydanticJob
from letta.schemas.job import JobUpdate, LettaRequestConfig
from letta.schemas.letta_message import UpdateAssistantMessage, UpdateReasoningMessage, UpdateSystemMessage, UpdateUserMessage
from letta.schemas.letta_message_content import TextContent
from letta.schemas.llm_batch_job import LLMBatchItem
from letta.schemas.llm_config import LLMConfig
from letta.schemas.message import Message as PydanticMessage
from letta.schemas.message import MessageCreate, MessageUpdate
from letta.schemas.openai.chat_completion_response import UsageStatistics
from letta.schemas.organization import Organization
from letta.schemas.organization import Organization as PydanticOrganization
from letta.schemas.organization import OrganizationUpdate
from letta.schemas.passage import Passage as PydanticPassage
from letta.schemas.run import Run as PydanticRun
from letta.schemas.sandbox_config import E2BSandboxConfig, LocalSandboxConfig, SandboxConfigCreate, SandboxConfigUpdate, SandboxType
from letta.schemas.source import Source as PydanticSource
from letta.schemas.source import SourceUpdate
from letta.schemas.tool import Tool as PydanticTool
from letta.schemas.tool import ToolCreate, ToolUpdate
from letta.schemas.tool_rule import InitToolRule
from letta.schemas.user import User as PydanticUser
from letta.schemas.user import UserUpdate
from letta.server.db import db_context
from letta.server.server import SyncServer
from letta.services.block_manager import BlockManager
from letta.services.organization_manager import OrganizationManager
from letta.settings import tool_settings
from tests.helpers.utils import comprehensive_agent_checks

DEFAULT_EMBEDDING_CONFIG = EmbeddingConfig(
    embedding_endpoint_type="hugging-face",
    embedding_endpoint="https://embeddings.memgpt.ai",
    embedding_model="letta-free",
    embedding_dim=1024,
    embedding_chunk_size=300,
    azure_endpoint=None,
    azure_version=None,
    azure_deployment=None,
)
CREATE_DELAY_SQLITE = 1
USING_SQLITE = not bool(os.getenv("LETTA_PG_URI"))


@pytest.fixture(autouse=True)
def _clear_tables():
    with db_context() as session:
        for table in reversed(Base.metadata.sorted_tables):  # Reverse to avoid FK issues
            # If this is the block_history table, skip it
            if table.name == "block_history":
                continue
            session.execute(table.delete())  # Truncate table
        session.commit()


@pytest.fixture
def default_organization(server: SyncServer):
    """Fixture to create and return the default organization."""
    org = server.organization_manager.create_default_organization()
    yield org


@pytest.fixture
def other_organization(server: SyncServer):
    """Fixture to create and return the default organization."""
    org = server.organization_manager.create_organization(pydantic_org=Organization(name="letta"))
    yield org


@pytest.fixture
def default_user(server: SyncServer, default_organization):
    """Fixture to create and return the default user within the default organization."""
    user = server.user_manager.create_default_user(org_id=default_organization.id)
    yield user


@pytest.fixture
def other_user(server: SyncServer, default_organization):
    """Fixture to create and return the default user within the default organization."""
    user = server.user_manager.create_user(PydanticUser(name="other", organization_id=default_organization.id))
    yield user


@pytest.fixture
def other_user_different_org(server: SyncServer, other_organization):
    """Fixture to create and return the default user within the default organization."""
    user = server.user_manager.create_user(PydanticUser(name="other", organization_id=other_organization.id))
    yield user


@pytest.fixture
def default_source(server: SyncServer, default_user):
    source_pydantic = PydanticSource(
        name="Test Source",
        description="This is a test source.",
        metadata={"type": "test"},
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)
    yield source


@pytest.fixture
def other_source(server: SyncServer, default_user):
    source_pydantic = PydanticSource(
        name="Another Test Source",
        description="This is yet another test source.",
        metadata={"type": "another_test"},
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)
    yield source


@pytest.fixture
def default_file(server: SyncServer, default_source, default_user, default_organization):
    file = server.source_manager.create_file(
        PydanticFileMetadata(file_name="test_file", organization_id=default_organization.id, source_id=default_source.id),
        actor=default_user,
    )
    yield file


@pytest.fixture
def print_tool(server: SyncServer, default_user, default_organization):
    """Fixture to create a tool with default settings and clean up after the test."""

    def print_tool(message: str):
        """
        Args:
            message (str): The message to print.

        Returns:
            str: The message that was printed.
        """
        print(message)
        return message

    # Set up tool details
    source_code = parse_source_code(print_tool)
    source_type = "python"
    description = "test_description"
    tags = ["test"]
    metadata = {"a": "b"}

    tool = PydanticTool(description=description, tags=tags, source_code=source_code, source_type=source_type, metadata_=metadata)
    derived_json_schema = derive_openai_json_schema(source_code=tool.source_code, name=tool.name)

    derived_name = derived_json_schema["name"]
    tool.json_schema = derived_json_schema
    tool.name = derived_name

    tool = server.tool_manager.create_tool(tool, actor=default_user)

    # Yield the created tool
    yield tool


@pytest.fixture
def composio_github_star_tool(server, default_user):
    tool_create = ToolCreate.from_composio(action_name="GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER")
    tool = server.tool_manager.create_or_update_composio_tool(tool_create=tool_create, actor=default_user)
    yield tool


@pytest.fixture
def mcp_tool(server, default_user):
    mcp_tool = MCPTool(
        name="weather_lookup",
        description="Fetches the current weather for a given location.",
        inputSchema={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "The name of the city or location."},
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "The unit system for temperature (metric or imperial).",
                },
            },
            "required": ["location"],
        },
    )
    mcp_server_name = "test"
    tool_create = ToolCreate.from_mcp(mcp_server_name=mcp_server_name, mcp_tool=mcp_tool)
    tool = server.tool_manager.create_or_update_mcp_tool(tool_create=tool_create, mcp_server_name=mcp_server_name, actor=default_user)
    yield tool


@pytest.fixture
def default_job(server: SyncServer, default_user):
    """Fixture to create and return a default job."""
    job_pydantic = PydanticJob(
        user_id=default_user.id,
        status=JobStatus.pending,
    )
    job = server.job_manager.create_job(pydantic_job=job_pydantic, actor=default_user)
    yield job


@pytest.fixture
def default_run(server: SyncServer, default_user):
    """Fixture to create and return a default job."""
    run_pydantic = PydanticRun(
        user_id=default_user.id,
        status=JobStatus.pending,
    )
    run = server.job_manager.create_job(pydantic_job=run_pydantic, actor=default_user)
    yield run


@pytest.fixture
def agent_passage_fixture(server: SyncServer, default_user, sarah_agent):
    """Fixture to create an agent passage."""
    passage = server.passage_manager.create_passage(
        PydanticPassage(
            text="Hello, I am an agent passage",
            agent_id=sarah_agent.id,
            organization_id=default_user.organization_id,
            embedding=[0.1],
            embedding_config=DEFAULT_EMBEDDING_CONFIG,
            metadata={"type": "test"},
        ),
        actor=default_user,
    )
    yield passage


@pytest.fixture
def source_passage_fixture(server: SyncServer, default_user, default_file, default_source):
    """Fixture to create a source passage."""
    passage = server.passage_manager.create_passage(
        PydanticPassage(
            text="Hello, I am a source passage",
            source_id=default_source.id,
            file_id=default_file.id,
            organization_id=default_user.organization_id,
            embedding=[0.1],
            embedding_config=DEFAULT_EMBEDDING_CONFIG,
            metadata={"type": "test"},
        ),
        actor=default_user,
    )
    yield passage


@pytest.fixture
def create_test_passages(server: SyncServer, default_file, default_user, sarah_agent, default_source):
    """Helper function to create test passages for all tests."""
    # Create agent passages
    passages = []
    for i in range(5):
        passage = server.passage_manager.create_passage(
            PydanticPassage(
                text=f"Agent passage {i}",
                agent_id=sarah_agent.id,
                organization_id=default_user.organization_id,
                embedding=[0.1],
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
                metadata={"type": "test"},
            ),
            actor=default_user,
        )
        passages.append(passage)
        if USING_SQLITE:
            time.sleep(CREATE_DELAY_SQLITE)

    # Create source passages
    for i in range(5):
        passage = server.passage_manager.create_passage(
            PydanticPassage(
                text=f"Source passage {i}",
                source_id=default_source.id,
                file_id=default_file.id,
                organization_id=default_user.organization_id,
                embedding=[0.1],
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
                metadata={"type": "test"},
            ),
            actor=default_user,
        )
        passages.append(passage)
        if USING_SQLITE:
            time.sleep(CREATE_DELAY_SQLITE)

    return passages


@pytest.fixture
def hello_world_message_fixture(server: SyncServer, default_user, sarah_agent):
    """Fixture to create a tool with default settings and clean up after the test."""
    # Set up message
    message = PydanticMessage(
        organization_id=default_user.organization_id,
        agent_id=sarah_agent.id,
        role="user",
        content=[TextContent(text="Hello, world!")],
    )

    msg = server.message_manager.create_message(message, actor=default_user)
    yield msg


@pytest.fixture
def sandbox_config_fixture(server: SyncServer, default_user):
    sandbox_config_create = SandboxConfigCreate(
        config=E2BSandboxConfig(),
    )
    created_config = server.sandbox_config_manager.create_or_update_sandbox_config(sandbox_config_create, actor=default_user)
    yield created_config


@pytest.fixture
def sandbox_env_var_fixture(server: SyncServer, sandbox_config_fixture, default_user):
    env_var_create = SandboxEnvironmentVariableCreate(
        key="SAMPLE_VAR",
        value="sample_value",
        description="A sample environment variable for testing.",
    )
    created_env_var = server.sandbox_config_manager.create_sandbox_env_var(
        env_var_create, sandbox_config_id=sandbox_config_fixture.id, actor=default_user
    )
    yield created_env_var


@pytest.fixture
def default_block(server: SyncServer, default_user):
    """Fixture to create and return a default block."""
    block_manager = BlockManager()
    block_data = PydanticBlock(
        label="default_label",
        value="Default Block Content",
        description="A default test block",
        limit=1000,
        metadata={"type": "test"},
    )
    block = block_manager.create_or_update_block(block_data, actor=default_user)
    yield block


@pytest.fixture
def other_block(server: SyncServer, default_user):
    """Fixture to create and return another block."""
    block_manager = BlockManager()
    block_data = PydanticBlock(
        label="other_label",
        value="Other Block Content",
        description="Another test block",
        limit=500,
        metadata={"type": "test"},
    )
    block = block_manager.create_or_update_block(block_data, actor=default_user)
    yield block


@pytest.fixture
def other_tool(server: SyncServer, default_user, default_organization):
    def print_other_tool(message: str):
        """
        Args:
            message (str): The message to print.

        Returns:
            str: The message that was printed.
        """
        print(message)
        return message

    # Set up tool details
    source_code = parse_source_code(print_other_tool)
    source_type = "python"
    description = "other_tool_description"
    tags = ["test"]

    tool = PydanticTool(description=description, tags=tags, source_code=source_code, source_type=source_type)
    derived_json_schema = derive_openai_json_schema(source_code=tool.source_code, name=tool.name)

    derived_name = derived_json_schema["name"]
    tool.json_schema = derived_json_schema
    tool.name = derived_name

    tool = server.tool_manager.create_tool(tool, actor=default_user)

    # Yield the created tool
    yield tool


@pytest.fixture
def sarah_agent(server: SyncServer, default_user, default_organization):
    """Fixture to create and return a sample agent within the default organization."""
    agent_state = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="sarah_agent",
            memory_blocks=[],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )
    yield agent_state


@pytest.fixture
def charles_agent(server: SyncServer, default_user, default_organization):
    """Fixture to create and return a sample agent within the default organization."""
    agent_state = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="charles_agent",
            memory_blocks=[CreateBlock(label="human", value="Charles"), CreateBlock(label="persona", value="I am a helpful assistant")],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )
    yield agent_state


@pytest.fixture
def comprehensive_test_agent_fixture(server: SyncServer, default_user, print_tool, default_source, default_block):
    memory_blocks = [CreateBlock(label="human", value="BananaBoy"), CreateBlock(label="persona", value="I am a helpful assistant")]
    create_agent_request = CreateAgent(
        system="test system",
        memory_blocks=memory_blocks,
        llm_config=LLMConfig.default_config("gpt-4"),
        embedding_config=EmbeddingConfig.default_config(provider="openai"),
        block_ids=[default_block.id],
        tool_ids=[print_tool.id],
        source_ids=[default_source.id],
        tags=["a", "b"],
        description="test_description",
        metadata={"test_key": "test_value"},
        tool_rules=[InitToolRule(tool_name=print_tool.name)],
        initial_message_sequence=[MessageCreate(role=MessageRole.user, content="hello world")],
        tool_exec_environment_variables={"test_env_var_key_a": "test_env_var_value_a", "test_env_var_key_b": "test_env_var_value_b"},
        message_buffer_autoclear=True,
        include_base_tools=False,
    )
    created_agent = server.agent_manager.create_agent(
        create_agent_request,
        actor=default_user,
    )

    yield created_agent, create_agent_request


@pytest.fixture(scope="module")
def server():
    config = LettaConfig.load()

    config.save()

    server = SyncServer(init_with_default_org_and_user=False)
    return server


@pytest.fixture
def agent_passages_setup(server, default_source, default_user, sarah_agent):
    """Setup fixture for agent passages tests"""
    agent_id = sarah_agent.id
    actor = default_user

    server.agent_manager.attach_source(agent_id=agent_id, source_id=default_source.id, actor=actor)

    # Create some source passages
    source_passages = []
    for i in range(3):
        passage = server.passage_manager.create_passage(
            PydanticPassage(
                organization_id=actor.organization_id,
                source_id=default_source.id,
                text=f"Source passage {i}",
                embedding=[0.1],  # Default OpenAI embedding size
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
            ),
            actor=actor,
        )
        source_passages.append(passage)

    # Create some agent passages
    agent_passages = []
    for i in range(2):
        passage = server.passage_manager.create_passage(
            PydanticPassage(
                organization_id=actor.organization_id,
                agent_id=agent_id,
                text=f"Agent passage {i}",
                embedding=[0.1],  # Default OpenAI embedding size
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
            ),
            actor=actor,
        )
        agent_passages.append(passage)

    yield agent_passages, source_passages

    # Cleanup
    server.source_manager.delete_source(default_source.id, actor=actor)


@pytest.fixture
def agent_with_tags(server: SyncServer, default_user):
    """Fixture to create agents with specific tags."""
    agent1 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent1",
            tags=["primary_agent", "benefit_1"],
            llm_config=LLMConfig.default_config("gpt-4o-mini"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agent2 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent2",
            tags=["primary_agent", "benefit_2"],
            llm_config=LLMConfig.default_config("gpt-4o-mini"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agent3 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent3",
            tags=["primary_agent", "benefit_1", "benefit_2"],
            llm_config=LLMConfig.default_config("gpt-4o-mini"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    return [agent1, agent2, agent3]


@pytest.fixture
def dummy_llm_config() -> LLMConfig:
    return LLMConfig.default_config("gpt-4")


@pytest.fixture
def dummy_tool_rules_solver() -> ToolRulesSolver:
    return ToolRulesSolver(tool_rules=[InitToolRule(tool_name="send_message")])


@pytest.fixture
def dummy_step_state(dummy_tool_rules_solver: ToolRulesSolver) -> AgentStepState:
    return AgentStepState(step_number=1, tool_rules_solver=dummy_tool_rules_solver)


@pytest.fixture
def dummy_successful_response() -> BetaMessageBatchIndividualResponse:
    return BetaMessageBatchIndividualResponse(
        custom_id="my-second-request",
        result=BetaMessageBatchSucceededResult(
            type="succeeded",
            message=BetaMessage(
                id="msg_abc123",
                role="assistant",
                type="message",
                model="claude-3-5-sonnet-20240620",
                content=[{"type": "text", "text": "hi!"}],
                usage={"input_tokens": 5, "output_tokens": 7},
                stop_reason="end_turn",
            ),
        ),
    )


@pytest.fixture
def letta_batch_job(server: SyncServer, default_user) -> Job:
    return server.job_manager.create_job(BatchJob(user_id=default_user.id), actor=default_user)


# ======================================================================================================================
# AgentManager Tests - Basic
# ======================================================================================================================
def test_create_get_list_agent(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    # Test agent creation
    created_agent, create_agent_request = comprehensive_test_agent_fixture
    comprehensive_agent_checks(created_agent, create_agent_request, actor=default_user)

    # Test get agent
    get_agent = server.agent_manager.get_agent_by_id(agent_id=created_agent.id, actor=default_user)
    comprehensive_agent_checks(get_agent, create_agent_request, actor=default_user)

    # Test get agent name
    get_agent_name = server.agent_manager.get_agent_by_name(agent_name=created_agent.name, actor=default_user)
    comprehensive_agent_checks(get_agent_name, create_agent_request, actor=default_user)

    # Test list agent
    list_agents = server.agent_manager.list_agents(actor=default_user)
    assert len(list_agents) == 1
    comprehensive_agent_checks(list_agents[0], create_agent_request, actor=default_user)

    # Test deleting the agent
    server.agent_manager.delete_agent(get_agent.id, default_user)
    list_agents = server.agent_manager.list_agents(actor=default_user)
    assert len(list_agents) == 0


def test_create_agent_passed_in_initial_messages(server: SyncServer, default_user, default_block):
    memory_blocks = [CreateBlock(label="human", value="BananaBoy"), CreateBlock(label="persona", value="I am a helpful assistant")]
    create_agent_request = CreateAgent(
        system="test system",
        memory_blocks=memory_blocks,
        llm_config=LLMConfig.default_config("gpt-4"),
        embedding_config=EmbeddingConfig.default_config(provider="openai"),
        block_ids=[default_block.id],
        tags=["a", "b"],
        description="test_description",
        initial_message_sequence=[MessageCreate(role=MessageRole.user, content="hello world")],
        include_base_tools=False,
    )
    agent_state = server.agent_manager.create_agent(
        create_agent_request,
        actor=default_user,
    )
    assert server.message_manager.size(agent_id=agent_state.id, actor=default_user) == 2
    init_messages = server.agent_manager.get_in_context_messages(agent_id=agent_state.id, actor=default_user)
    # Check that the system appears in the first initial message
    assert create_agent_request.system in init_messages[0].content[0].text
    assert create_agent_request.memory_blocks[0].value in init_messages[0].content[0].text
    # Check that the second message is the passed in initial message seq
    assert create_agent_request.initial_message_sequence[0].role == init_messages[1].role
    assert create_agent_request.initial_message_sequence[0].content in init_messages[1].content[0].text


def test_create_agent_default_initial_message(server: SyncServer, default_user, default_block):
    memory_blocks = [CreateBlock(label="human", value="BananaBoy"), CreateBlock(label="persona", value="I am a helpful assistant")]
    create_agent_request = CreateAgent(
        system="test system",
        memory_blocks=memory_blocks,
        llm_config=LLMConfig.default_config("gpt-4"),
        embedding_config=EmbeddingConfig.default_config(provider="openai"),
        block_ids=[default_block.id],
        tags=["a", "b"],
        description="test_description",
        include_base_tools=False,
    )
    agent_state = server.agent_manager.create_agent(
        create_agent_request,
        actor=default_user,
    )
    assert server.message_manager.size(agent_id=agent_state.id, actor=default_user) == 4
    init_messages = server.agent_manager.get_in_context_messages(agent_id=agent_state.id, actor=default_user)
    # Check that the system appears in the first initial message
    assert create_agent_request.system in init_messages[0].content[0].text
    assert create_agent_request.memory_blocks[0].value in init_messages[0].content[0].text


def test_create_agent_with_json_in_system_message(server: SyncServer, default_user, default_block):
    system_prompt = (
        "You are an expert teaching agent with encyclopedic knowledge. "
        "When you receive a topic, query the external database for more "
        "information. Format the queries as a JSON list of queries making "
        "sure to include your reasoning for that query, e.g. "
        "{'query1' : 'reason1', 'query2' : 'reason2'}"
    )
    create_agent_request = CreateAgent(
        system=system_prompt,
        llm_config=LLMConfig.default_config("gpt-4"),
        embedding_config=EmbeddingConfig.default_config(provider="openai"),
        block_ids=[default_block.id],
        tags=["a", "b"],
        description="test_description",
        include_base_tools=False,
    )
    agent_state = server.agent_manager.create_agent(
        create_agent_request,
        actor=default_user,
    )
    assert agent_state is not None
    system_message_id = agent_state.message_ids[0]
    system_message = server.message_manager.get_message_by_id(message_id=system_message_id, actor=default_user)
    assert system_prompt in system_message.content[0].text
    assert default_block.value in system_message.content[0].text
    server.agent_manager.delete_agent(agent_id=agent_state.id, actor=default_user)


def test_update_agent(server: SyncServer, comprehensive_test_agent_fixture, other_tool, other_source, other_block, default_user):
    agent, _ = comprehensive_test_agent_fixture
    update_agent_request = UpdateAgent(
        name="train_agent",
        description="train description",
        tool_ids=[other_tool.id],
        source_ids=[other_source.id],
        block_ids=[other_block.id],
        tool_rules=[InitToolRule(tool_name=other_tool.name)],
        tags=["c", "d"],
        system="train system",
        llm_config=LLMConfig.default_config("gpt-4o-mini"),
        embedding_config=EmbeddingConfig.default_config(model_name="letta"),
        message_ids=["10", "20"],
        metadata={"train_key": "train_value"},
        tool_exec_environment_variables={"test_env_var_key_a": "a", "new_tool_exec_key": "n"},
        message_buffer_autoclear=False,
    )

    last_updated_timestamp = agent.updated_at
    updated_agent = server.agent_manager.update_agent(agent.id, update_agent_request, actor=default_user)
    comprehensive_agent_checks(updated_agent, update_agent_request, actor=default_user)
    assert updated_agent.message_ids == update_agent_request.message_ids
    assert updated_agent.updated_at > last_updated_timestamp


# ======================================================================================================================
# AgentManager Tests - Listing
# ======================================================================================================================


def test_list_agents_select_fields_empty(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    # Create an agent using the comprehensive fixture.
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # List agents using an empty list for select_fields.
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=[])
    # Assert that the agent is returned and basic fields are present.
    assert len(agents) >= 1
    agent = agents[0]
    assert agent.id is not None
    assert agent.name is not None

    # Assert no relationships were loaded
    assert len(agent.tools) == 0
    assert len(agent.tags) == 0


def test_list_agents_select_fields_none(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    # Create an agent using the comprehensive fixture.
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # List agents using an empty list for select_fields.
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=None)
    # Assert that the agent is returned and basic fields are present.
    assert len(agents) >= 1
    agent = agents[0]
    assert agent.id is not None
    assert agent.name is not None

    # Assert no relationships were loaded
    assert len(agent.tools) > 0
    assert len(agent.tags) > 0


def test_list_agents_select_fields_specific(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # Choose a subset of valid relationship fields.
    valid_fields = ["tools", "tags"]
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=valid_fields)
    assert len(agents) >= 1
    agent = agents[0]
    # Depending on your to_pydantic() implementation,
    # verify that the fields exist in the returned pydantic model.
    # (Note: These assertions may require that your CreateAgent fixture sets up these relationships.)
    assert agent.tools
    assert sorted(agent.tags) == ["a", "b"]
    assert not agent.memory.blocks


def test_list_agents_select_fields_invalid(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # Provide field names that are not recognized.
    invalid_fields = ["foobar", "nonexistent_field"]
    # The expectation is that these fields are simply ignored.
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=invalid_fields)
    assert len(agents) >= 1
    agent = agents[0]
    # Verify that standard fields are still present.c
    assert agent.id is not None
    assert agent.name is not None


def test_list_agents_select_fields_duplicates(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # Provide duplicate valid field names.
    duplicate_fields = ["tools", "tools", "tags", "tags"]
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=duplicate_fields)
    assert len(agents) >= 1
    agent = agents[0]
    # Verify that the agent pydantic representation includes the relationships.
    # Even if duplicates were provided, the query should not break.
    assert isinstance(agent.tools, list)
    assert isinstance(agent.tags, list)


def test_list_agents_select_fields_mixed(server: SyncServer, comprehensive_test_agent_fixture, default_user):
    created_agent, create_agent_request = comprehensive_test_agent_fixture

    # Mix valid fields with an invalid one.
    mixed_fields = ["tools", "invalid_field"]
    agents = server.agent_manager.list_agents(actor=default_user, include_relationships=mixed_fields)
    assert len(agents) >= 1
    agent = agents[0]
    # Valid fields should be loaded and accessible.
    assert agent.tools
    # Since "invalid_field" is not recognized, it should have no adverse effect.
    # You might optionally check that no extra attribute is created on the pydantic model.
    assert not hasattr(agent, "invalid_field")


def test_list_agents_ascending(server: SyncServer, default_user):
    # Create two agents with known names
    agent1 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent_oldest",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)

    agent2 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent_newest",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agents = server.agent_manager.list_agents(actor=default_user, ascending=True)
    names = [agent.name for agent in agents]
    assert names.index("agent_oldest") < names.index("agent_newest")


def test_list_agents_descending(server: SyncServer, default_user):
    # Create two agents with known names
    agent1 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent_oldest",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)

    agent2 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent_newest",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agents = server.agent_manager.list_agents(actor=default_user, ascending=False)
    names = [agent.name for agent in agents]
    assert names.index("agent_newest") < names.index("agent_oldest")


def test_list_agents_ordering_and_pagination(server: SyncServer, default_user):
    names = ["alpha_agent", "beta_agent", "gamma_agent"]
    created_agents = []

    # Create agents in known order
    for name in names:
        agent = server.agent_manager.create_agent(
            agent_create=CreateAgent(
                name=name,
                memory_blocks=[],
                llm_config=LLMConfig.default_config("gpt-4"),
                embedding_config=EmbeddingConfig.default_config(provider="openai"),
                include_base_tools=False,
            ),
            actor=default_user,
        )
        created_agents.append(agent)
        if USING_SQLITE:
            time.sleep(CREATE_DELAY_SQLITE)

    agent_ids = {agent.name: agent.id for agent in created_agents}

    # Ascending (oldest to newest)
    agents_asc = server.agent_manager.list_agents(actor=default_user, ascending=True)
    asc_names = [agent.name for agent in agents_asc]
    assert asc_names.index("alpha_agent") < asc_names.index("beta_agent") < asc_names.index("gamma_agent")

    # Descending (newest to oldest)
    agents_desc = server.agent_manager.list_agents(actor=default_user, ascending=False)
    desc_names = [agent.name for agent in agents_desc]
    assert desc_names.index("gamma_agent") < desc_names.index("beta_agent") < desc_names.index("alpha_agent")

    # After: Get agents after alpha_agent in ascending order (should exclude alpha)
    after_alpha = server.agent_manager.list_agents(actor=default_user, after=agent_ids["alpha_agent"], ascending=True)
    after_names = [a.name for a in after_alpha]
    assert "alpha_agent" not in after_names
    assert "beta_agent" in after_names
    assert "gamma_agent" in after_names
    assert after_names == ["beta_agent", "gamma_agent"]

    # Before: Get agents before gamma_agent in ascending order (should exclude gamma)
    before_gamma = server.agent_manager.list_agents(actor=default_user, before=agent_ids["gamma_agent"], ascending=True)
    before_names = [a.name for a in before_gamma]
    assert "gamma_agent" not in before_names
    assert "alpha_agent" in before_names
    assert "beta_agent" in before_names
    assert before_names == ["alpha_agent", "beta_agent"]

    # After: Get agents after gamma_agent in descending order (should exclude gamma, return beta then alpha)
    after_gamma_desc = server.agent_manager.list_agents(actor=default_user, after=agent_ids["gamma_agent"], ascending=False)
    after_names_desc = [a.name for a in after_gamma_desc]
    assert after_names_desc == ["beta_agent", "alpha_agent"]

    # Before: Get agents before alpha_agent in descending order (should exclude alpha)
    before_alpha_desc = server.agent_manager.list_agents(actor=default_user, before=agent_ids["alpha_agent"], ascending=False)
    before_names_desc = [a.name for a in before_alpha_desc]
    assert before_names_desc == ["gamma_agent", "beta_agent"]


# ======================================================================================================================
# AgentManager Tests - Tools Relationship
# ======================================================================================================================


def test_attach_tool(server: SyncServer, sarah_agent, print_tool, default_user):
    """Test attaching a tool to an agent."""
    # Attach the tool
    server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)

    # Verify attachment through get_agent_by_id
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert print_tool.id in [t.id for t in agent.tools]

    # Verify that attaching the same tool again doesn't cause duplication
    server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert len([t for t in agent.tools if t.id == print_tool.id]) == 1


def test_detach_tool(server: SyncServer, sarah_agent, print_tool, default_user):
    """Test detaching a tool from an agent."""
    # Attach the tool first
    server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)

    # Verify it's attached
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert print_tool.id in [t.id for t in agent.tools]

    # Detach the tool
    server.agent_manager.detach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)

    # Verify it's detached
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert print_tool.id not in [t.id for t in agent.tools]

    # Verify that detaching an already detached tool doesn't cause issues
    server.agent_manager.detach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)


def test_attach_tool_nonexistent_agent(server: SyncServer, print_tool, default_user):
    """Test attaching a tool to a nonexistent agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.attach_tool(agent_id="nonexistent-agent-id", tool_id=print_tool.id, actor=default_user)


def test_attach_tool_nonexistent_tool(server: SyncServer, sarah_agent, default_user):
    """Test attaching a nonexistent tool to an agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id="nonexistent-tool-id", actor=default_user)


def test_detach_tool_nonexistent_agent(server: SyncServer, print_tool, default_user):
    """Test detaching a tool from a nonexistent agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.detach_tool(agent_id="nonexistent-agent-id", tool_id=print_tool.id, actor=default_user)


def test_list_attached_tools(server: SyncServer, sarah_agent, print_tool, other_tool, default_user):
    """Test listing tools attached to an agent."""
    # Initially should have no tools
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert len(agent.tools) == 0

    # Attach tools
    server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id=print_tool.id, actor=default_user)
    server.agent_manager.attach_tool(agent_id=sarah_agent.id, tool_id=other_tool.id, actor=default_user)

    # List tools and verify
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    attached_tool_ids = [t.id for t in agent.tools]
    assert len(attached_tool_ids) == 2
    assert print_tool.id in attached_tool_ids
    assert other_tool.id in attached_tool_ids


# ======================================================================================================================
# AgentManager Tests - Sources Relationship
# ======================================================================================================================


def test_attach_source(server: SyncServer, sarah_agent, default_source, default_user):
    """Test attaching a source to an agent."""
    # Attach the source
    server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id=default_source.id, actor=default_user)

    # Verify attachment through get_agent_by_id
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert default_source.id in [s.id for s in agent.sources]

    # Verify that attaching the same source again doesn't cause issues
    server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id=default_source.id, actor=default_user)
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert len([s for s in agent.sources if s.id == default_source.id]) == 1


def test_list_attached_source_ids(server: SyncServer, sarah_agent, default_source, other_source, default_user):
    """Test listing source IDs attached to an agent."""
    # Initially should have no sources
    sources = server.agent_manager.list_attached_sources(sarah_agent.id, actor=default_user)
    assert len(sources) == 0

    # Attach sources
    server.agent_manager.attach_source(sarah_agent.id, default_source.id, actor=default_user)
    server.agent_manager.attach_source(sarah_agent.id, other_source.id, actor=default_user)

    # List sources and verify
    sources = server.agent_manager.list_attached_sources(sarah_agent.id, actor=default_user)
    assert len(sources) == 2
    source_ids = [s.id for s in sources]
    assert default_source.id in source_ids
    assert other_source.id in source_ids


def test_detach_source(server: SyncServer, sarah_agent, default_source, default_user):
    """Test detaching a source from an agent."""
    # Attach source
    server.agent_manager.attach_source(sarah_agent.id, default_source.id, actor=default_user)

    # Verify it's attached
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert default_source.id in [s.id for s in agent.sources]

    # Detach source
    server.agent_manager.detach_source(sarah_agent.id, default_source.id, actor=default_user)

    # Verify it's detached
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert default_source.id not in [s.id for s in agent.sources]

    # Verify that detaching an already detached source doesn't cause issues
    server.agent_manager.detach_source(sarah_agent.id, default_source.id, actor=default_user)


def test_attach_source_nonexistent_agent(server: SyncServer, default_source, default_user):
    """Test attaching a source to a nonexistent agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.attach_source(agent_id="nonexistent-agent-id", source_id=default_source.id, actor=default_user)


def test_attach_source_nonexistent_source(server: SyncServer, sarah_agent, default_user):
    """Test attaching a nonexistent source to an agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id="nonexistent-source-id", actor=default_user)


def test_detach_source_nonexistent_agent(server: SyncServer, default_source, default_user):
    """Test detaching a source from a nonexistent agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.detach_source(agent_id="nonexistent-agent-id", source_id=default_source.id, actor=default_user)


def test_list_attached_source_ids_nonexistent_agent(server: SyncServer, default_user):
    """Test listing sources for a nonexistent agent."""
    with pytest.raises(NoResultFound):
        server.agent_manager.list_attached_sources(agent_id="nonexistent-agent-id", actor=default_user)


def test_list_attached_agents(server: SyncServer, sarah_agent, charles_agent, default_source, default_user):
    """Test listing agents that have a particular source attached."""
    # Initially should have no attached agents
    attached_agents = server.source_manager.list_attached_agents(source_id=default_source.id, actor=default_user)
    assert len(attached_agents) == 0

    # Attach source to first agent
    server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id=default_source.id, actor=default_user)

    # Verify one agent is now attached
    attached_agents = server.source_manager.list_attached_agents(source_id=default_source.id, actor=default_user)
    assert len(attached_agents) == 1
    assert sarah_agent.id in [a.id for a in attached_agents]

    # Attach source to second agent
    server.agent_manager.attach_source(agent_id=charles_agent.id, source_id=default_source.id, actor=default_user)

    # Verify both agents are now attached
    attached_agents = server.source_manager.list_attached_agents(source_id=default_source.id, actor=default_user)
    assert len(attached_agents) == 2
    attached_agent_ids = [a.id for a in attached_agents]
    assert sarah_agent.id in attached_agent_ids
    assert charles_agent.id in attached_agent_ids

    # Detach source from first agent
    server.agent_manager.detach_source(agent_id=sarah_agent.id, source_id=default_source.id, actor=default_user)

    # Verify only second agent remains attached
    attached_agents = server.source_manager.list_attached_agents(source_id=default_source.id, actor=default_user)
    assert len(attached_agents) == 1
    assert charles_agent.id in [a.id for a in attached_agents]


def test_list_attached_agents_nonexistent_source(server: SyncServer, default_user):
    """Test listing agents for a nonexistent source."""
    with pytest.raises(NoResultFound):
        server.source_manager.list_attached_agents(source_id="nonexistent-source-id", actor=default_user)


# ======================================================================================================================
# AgentManager Tests - Tags Relationship
# ======================================================================================================================


def test_list_agents_matching_all_tags(server: SyncServer, default_user, agent_with_tags):
    agents = server.agent_manager.list_agents_matching_tags(
        actor=default_user,
        match_all=["primary_agent", "benefit_1"],
        match_some=[],
    )
    assert len(agents) == 2  # agent1 and agent3 match
    assert {a.name for a in agents} == {"agent1", "agent3"}


def test_list_agents_matching_some_tags(server: SyncServer, default_user, agent_with_tags):
    agents = server.agent_manager.list_agents_matching_tags(
        actor=default_user,
        match_all=["primary_agent"],
        match_some=["benefit_1", "benefit_2"],
    )
    assert len(agents) == 3  # All agents match
    assert {a.name for a in agents} == {"agent1", "agent2", "agent3"}


def test_list_agents_matching_all_and_some_tags(server: SyncServer, default_user, agent_with_tags):
    agents = server.agent_manager.list_agents_matching_tags(
        actor=default_user,
        match_all=["primary_agent", "benefit_1"],
        match_some=["benefit_2", "nonexistent"],
    )
    assert len(agents) == 1  # Only agent3 matches
    assert agents[0].name == "agent3"


def test_list_agents_matching_no_tags(server: SyncServer, default_user, agent_with_tags):
    agents = server.agent_manager.list_agents_matching_tags(
        actor=default_user,
        match_all=["primary_agent", "nonexistent_tag"],
        match_some=["benefit_1", "benefit_2"],
    )
    assert len(agents) == 0  # No agent should match


def test_list_agents_by_tags_match_all(server: SyncServer, sarah_agent, charles_agent, default_user):
    """Test listing agents that have ALL specified tags."""
    # Create agents with multiple tags
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(tags=["test", "production", "gpt4"]), actor=default_user)
    server.agent_manager.update_agent(charles_agent.id, UpdateAgent(tags=["test", "development", "gpt4"]), actor=default_user)

    # Search for agents with all specified tags
    agents = server.agent_manager.list_agents(tags=["test", "gpt4"], match_all_tags=True, actor=default_user)
    assert len(agents) == 2
    agent_ids = [a.id for a in agents]
    assert sarah_agent.id in agent_ids
    assert charles_agent.id in agent_ids

    # Search for tags that only sarah_agent has
    agents = server.agent_manager.list_agents(tags=["test", "production"], match_all_tags=True, actor=default_user)
    assert len(agents) == 1
    assert agents[0].id == sarah_agent.id


def test_list_agents_by_tags_match_any(server: SyncServer, sarah_agent, charles_agent, default_user):
    """Test listing agents that have ANY of the specified tags."""
    # Create agents with different tags
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(tags=["production", "gpt4"]), actor=default_user)
    server.agent_manager.update_agent(charles_agent.id, UpdateAgent(tags=["development", "gpt3"]), actor=default_user)

    # Search for agents with any of the specified tags
    agents = server.agent_manager.list_agents(tags=["production", "development"], match_all_tags=False, actor=default_user)
    assert len(agents) == 2
    agent_ids = [a.id for a in agents]
    assert sarah_agent.id in agent_ids
    assert charles_agent.id in agent_ids

    # Search for tags where only sarah_agent matches
    agents = server.agent_manager.list_agents(tags=["production", "nonexistent"], match_all_tags=False, actor=default_user)
    assert len(agents) == 1
    assert agents[0].id == sarah_agent.id


def test_list_agents_by_tags_no_matches(server: SyncServer, sarah_agent, charles_agent, default_user):
    """Test listing agents when no tags match."""
    # Create agents with tags
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(tags=["production", "gpt4"]), actor=default_user)
    server.agent_manager.update_agent(charles_agent.id, UpdateAgent(tags=["development", "gpt3"]), actor=default_user)

    # Search for nonexistent tags
    agents = server.agent_manager.list_agents(tags=["nonexistent1", "nonexistent2"], match_all_tags=True, actor=default_user)
    assert len(agents) == 0

    agents = server.agent_manager.list_agents(tags=["nonexistent1", "nonexistent2"], match_all_tags=False, actor=default_user)
    assert len(agents) == 0


def test_list_agents_by_tags_with_other_filters(server: SyncServer, sarah_agent, charles_agent, default_user):
    """Test combining tag search with other filters."""
    # Create agents with specific names and tags
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(name="production_agent", tags=["production", "gpt4"]), actor=default_user)
    server.agent_manager.update_agent(charles_agent.id, UpdateAgent(name="test_agent", tags=["production", "gpt3"]), actor=default_user)

    # List agents with specific tag and name pattern
    agents = server.agent_manager.list_agents(actor=default_user, tags=["production"], match_all_tags=True, name="production_agent")
    assert len(agents) == 1
    assert agents[0].id == sarah_agent.id


def test_list_agents_by_tags_pagination(server: SyncServer, default_user, default_organization):
    """Test pagination when listing agents by tags."""
    # Create first agent
    agent1 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent1",
            tags=["pagination_test", "tag1"],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)  # Ensure distinct created_at timestamps

    # Create second agent
    agent2 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="agent2",
            tags=["pagination_test", "tag2"],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            memory_blocks=[],
            include_base_tools=False,
        ),
        actor=default_user,
    )

    # Get first page
    first_page = server.agent_manager.list_agents(tags=["pagination_test"], match_all_tags=True, actor=default_user, limit=1)
    assert len(first_page) == 1
    first_agent_id = first_page[0].id

    # Get second page using cursor
    second_page = server.agent_manager.list_agents(
        tags=["pagination_test"], match_all_tags=True, actor=default_user, after=first_agent_id, limit=1
    )
    assert len(second_page) == 1
    assert second_page[0].id != first_agent_id

    # Get previous page using before
    prev_page = server.agent_manager.list_agents(
        tags=["pagination_test"], match_all_tags=True, actor=default_user, before=second_page[0].id, limit=1
    )
    assert len(prev_page) == 1
    assert prev_page[0].id == first_agent_id

    # Verify we got both agents with no duplicates
    all_ids = {first_page[0].id, second_page[0].id}
    assert len(all_ids) == 2
    assert agent1.id in all_ids
    assert agent2.id in all_ids


def test_list_agents_query_text_pagination(server: SyncServer, default_user, default_organization):
    """Test listing agents with query text filtering and pagination."""
    # Create test agents with specific names and descriptions
    agent1 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="Search Agent One",
            memory_blocks=[],
            description="This is a search agent for testing",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agent2 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="Search Agent Two",
            memory_blocks=[],
            description="Another search agent for testing",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )

    agent3 = server.agent_manager.create_agent(
        agent_create=CreateAgent(
            name="Different Agent",
            memory_blocks=[],
            description="This is a different agent",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )

    # Test query text filtering
    search_results = server.agent_manager.list_agents(actor=default_user, query_text="search agent")
    assert len(search_results) == 2
    search_agent_ids = {agent.id for agent in search_results}
    assert agent1.id in search_agent_ids
    assert agent2.id in search_agent_ids
    assert agent3.id not in search_agent_ids

    different_results = server.agent_manager.list_agents(actor=default_user, query_text="different agent")
    assert len(different_results) == 1
    assert different_results[0].id == agent3.id

    # Test pagination with query text
    first_page = server.agent_manager.list_agents(actor=default_user, query_text="search agent", limit=1)
    assert len(first_page) == 1
    first_agent_id = first_page[0].id

    # Get second page using cursor
    second_page = server.agent_manager.list_agents(actor=default_user, query_text="search agent", after=first_agent_id, limit=1)
    assert len(second_page) == 1
    assert second_page[0].id != first_agent_id

    # Test before and after
    all_agents = server.agent_manager.list_agents(actor=default_user, query_text="agent")
    assert len(all_agents) == 3
    first_agent, second_agent, third_agent = all_agents
    middle_agent = server.agent_manager.list_agents(
        actor=default_user, query_text="search agent", before=third_agent.id, after=first_agent.id
    )
    assert len(middle_agent) == 1
    assert middle_agent[0].id == second_agent.id

    # Verify we got both search agents with no duplicates
    all_ids = {first_page[0].id, second_page[0].id}
    assert len(all_ids) == 2
    assert all_ids == {agent1.id, agent2.id}


# ======================================================================================================================
# AgentManager Tests - Messages Relationship
# ======================================================================================================================


def test_reset_messages_no_messages(server: SyncServer, sarah_agent, default_user):
    """
    Test that resetting messages on an agent that has zero messages
    does not fail and clears out message_ids if somehow it's non-empty.
    """
    # Force a weird scenario: Suppose the message_ids field was set non-empty (without actual messages).
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(message_ids=["ghost-message-id"]), actor=default_user)
    updated_agent = server.agent_manager.get_agent_by_id(sarah_agent.id, default_user)
    assert updated_agent.message_ids == ["ghost-message-id"]

    # Reset messages
    reset_agent = server.agent_manager.reset_messages(agent_id=sarah_agent.id, actor=default_user)
    assert len(reset_agent.message_ids) == 1
    # Double check that physically no messages exist
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 1


def test_reset_messages_default_messages(server: SyncServer, sarah_agent, default_user):
    """
    Test that resetting messages on an agent that has zero messages
    does not fail and clears out message_ids if somehow it's non-empty.
    """
    # Force a weird scenario: Suppose the message_ids field was set non-empty (without actual messages).
    server.agent_manager.update_agent(sarah_agent.id, UpdateAgent(message_ids=["ghost-message-id"]), actor=default_user)
    updated_agent = server.agent_manager.get_agent_by_id(sarah_agent.id, default_user)
    assert updated_agent.message_ids == ["ghost-message-id"]

    # Reset messages
    reset_agent = server.agent_manager.reset_messages(agent_id=sarah_agent.id, actor=default_user, add_default_initial_messages=True)
    assert len(reset_agent.message_ids) == 4
    # Double check that physically no messages exist
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 4


def test_reset_messages_with_existing_messages(server: SyncServer, sarah_agent, default_user):
    """
    Test that resetting messages on an agent with actual messages
    deletes them from the database and clears message_ids.
    """
    # 1. Create multiple messages for the agent
    msg1 = server.message_manager.create_message(
        PydanticMessage(
            agent_id=sarah_agent.id,
            organization_id=default_user.organization_id,
            role="user",
            content=[TextContent(text="Hello, Sarah!")],
        ),
        actor=default_user,
    )
    msg2 = server.message_manager.create_message(
        PydanticMessage(
            agent_id=sarah_agent.id,
            organization_id=default_user.organization_id,
            role="assistant",
            content=[TextContent(text="Hello, user!")],
        ),
        actor=default_user,
    )

    # Verify the messages were created
    agent_before = server.agent_manager.get_agent_by_id(sarah_agent.id, default_user)
    # This is 4 because creating the message does not necessarily add it to the in context message ids
    assert len(agent_before.message_ids) == 4
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 6

    # 2. Reset all messages
    reset_agent = server.agent_manager.reset_messages(agent_id=sarah_agent.id, actor=default_user)

    # 3. Verify the agent now has zero message_ids
    assert len(reset_agent.message_ids) == 1

    # 4. Verify the messages are physically removed
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 1


def test_reset_messages_idempotency(server: SyncServer, sarah_agent, default_user):
    """
    Test that calling reset_messages multiple times has no adverse effect.
    """
    # Create a single message
    server.message_manager.create_message(
        PydanticMessage(
            agent_id=sarah_agent.id,
            organization_id=default_user.organization_id,
            role="user",
            content=[TextContent(text="Hello, Sarah!")],
        ),
        actor=default_user,
    )
    # First reset
    reset_agent = server.agent_manager.reset_messages(agent_id=sarah_agent.id, actor=default_user)
    assert len(reset_agent.message_ids) == 1
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 1

    # Second reset should do nothing new
    reset_agent_again = server.agent_manager.reset_messages(agent_id=sarah_agent.id, actor=default_user)
    assert len(reset_agent.message_ids) == 1
    assert server.message_manager.size(agent_id=sarah_agent.id, actor=default_user) == 1


def test_modify_letta_message(server: SyncServer, sarah_agent, default_user):
    """
    Test updating a message.
    """

    messages = server.message_manager.list_messages_for_agent(agent_id=sarah_agent.id, actor=default_user)
    letta_messages = PydanticMessage.to_letta_messages_from_list(messages=messages)

    system_message = [msg for msg in letta_messages if msg.message_type == "system_message"][0]
    assistant_message = [msg for msg in letta_messages if msg.message_type == "assistant_message"][0]
    user_message = [msg for msg in letta_messages if msg.message_type == "user_message"][0]
    reasoning_message = [msg for msg in letta_messages if msg.message_type == "reasoning_message"][0]

    # user message
    update_user_message = UpdateUserMessage(content="Hello, Sarah!")
    original_user_message = server.message_manager.get_message_by_id(message_id=user_message.id, actor=default_user)
    assert original_user_message.content[0].text != update_user_message.content
    server.message_manager.update_message_by_letta_message(
        message_id=user_message.id, letta_message_update=update_user_message, actor=default_user
    )
    updated_user_message = server.message_manager.get_message_by_id(message_id=user_message.id, actor=default_user)
    assert updated_user_message.content[0].text == update_user_message.content

    # system message
    update_system_message = UpdateSystemMessage(content="You are a friendly assistant!")
    original_system_message = server.message_manager.get_message_by_id(message_id=system_message.id, actor=default_user)
    assert original_system_message.content[0].text != update_system_message.content
    server.message_manager.update_message_by_letta_message(
        message_id=system_message.id, letta_message_update=update_system_message, actor=default_user
    )
    updated_system_message = server.message_manager.get_message_by_id(message_id=system_message.id, actor=default_user)
    assert updated_system_message.content[0].text == update_system_message.content

    # reasoning message
    update_reasoning_message = UpdateReasoningMessage(reasoning="I am thinking")
    original_reasoning_message = server.message_manager.get_message_by_id(message_id=reasoning_message.id, actor=default_user)
    assert original_reasoning_message.content[0].text != update_reasoning_message.reasoning
    server.message_manager.update_message_by_letta_message(
        message_id=reasoning_message.id, letta_message_update=update_reasoning_message, actor=default_user
    )
    updated_reasoning_message = server.message_manager.get_message_by_id(message_id=reasoning_message.id, actor=default_user)
    assert updated_reasoning_message.content[0].text == update_reasoning_message.reasoning

    # assistant message
    def parse_send_message(tool_call):
        import json

        function_call = tool_call.function
        arguments = json.loads(function_call.arguments)
        return arguments["message"]

    update_assistant_message = UpdateAssistantMessage(content="I am an agent!")
    original_assistant_message = server.message_manager.get_message_by_id(message_id=assistant_message.id, actor=default_user)
    print("ORIGINAL", original_assistant_message.tool_calls)
    print("MESSAGE", parse_send_message(original_assistant_message.tool_calls[0]))
    assert parse_send_message(original_assistant_message.tool_calls[0]) != update_assistant_message.content
    server.message_manager.update_message_by_letta_message(
        message_id=assistant_message.id, letta_message_update=update_assistant_message, actor=default_user
    )
    updated_assistant_message = server.message_manager.get_message_by_id(message_id=assistant_message.id, actor=default_user)
    print("UPDATED", updated_assistant_message.tool_calls)
    print("MESSAGE", parse_send_message(updated_assistant_message.tool_calls[0]))
    assert parse_send_message(updated_assistant_message.tool_calls[0]) == update_assistant_message.content

    # TODO: tool calls/responses


# ======================================================================================================================
# AgentManager Tests - Blocks Relationship
# ======================================================================================================================


def test_attach_block(server: SyncServer, sarah_agent, default_block, default_user):
    """Test attaching a block to an agent."""
    # Attach block
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Verify attachment
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert len(agent.memory.blocks) == 1
    assert agent.memory.blocks[0].id == default_block.id
    assert agent.memory.blocks[0].label == default_block.label


@pytest.mark.skipif(USING_SQLITE, reason="Test not applicable when using SQLite.")
def test_attach_block_duplicate_label(server: SyncServer, sarah_agent, default_block, other_block, default_user):
    """Test attempting to attach a block with a duplicate label."""
    # Set up both blocks with same label
    server.block_manager.update_block(default_block.id, BlockUpdate(label="same_label"), actor=default_user)
    server.block_manager.update_block(other_block.id, BlockUpdate(label="same_label"), actor=default_user)

    # Attach first block
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Attempt to attach second block with same label
    with pytest.raises(IntegrityError):
        server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=other_block.id, actor=default_user)


def test_detach_block(server: SyncServer, sarah_agent, default_block, default_user):
    """Test detaching a block by ID."""
    # Set up: attach block
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Detach block
    server.agent_manager.detach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Verify detachment
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert len(agent.memory.blocks) == 0

    # Check that block still exists
    block = server.block_manager.get_block_by_id(block_id=default_block.id, actor=default_user)
    assert block


def test_detach_nonexistent_block(server: SyncServer, sarah_agent, default_user):
    """Test detaching a block that isn't attached."""
    with pytest.raises(NoResultFound):
        server.agent_manager.detach_block(agent_id=sarah_agent.id, block_id="nonexistent-block-id", actor=default_user)


def test_update_block_label(server: SyncServer, sarah_agent, default_block, default_user):
    """Test updating a block's label updates the relationship."""
    # Attach block
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Update block label
    new_label = "new_label"
    server.block_manager.update_block(default_block.id, BlockUpdate(label=new_label), actor=default_user)

    # Verify relationship is updated
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    block = agent.memory.blocks[0]
    assert block.id == default_block.id
    assert block.label == new_label


def test_update_block_label_multiple_agents(server: SyncServer, sarah_agent, charles_agent, default_block, default_user):
    """Test updating a block's label updates relationships for all agents."""
    # Attach block to both agents
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)
    server.agent_manager.attach_block(agent_id=charles_agent.id, block_id=default_block.id, actor=default_user)

    # Update block label
    new_label = "new_label"
    server.block_manager.update_block(default_block.id, BlockUpdate(label=new_label), actor=default_user)

    # Verify both relationships are updated
    for agent_id in [sarah_agent.id, charles_agent.id]:
        agent = server.agent_manager.get_agent_by_id(agent_id, actor=default_user)
        # Find our specific block by ID
        block = next(b for b in agent.memory.blocks if b.id == default_block.id)
        assert block.label == new_label


def test_get_block_with_label(server: SyncServer, sarah_agent, default_block, default_user):
    """Test retrieving a block by its label."""
    # Attach block
    server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=default_block.id, actor=default_user)

    # Get block by label
    block = server.agent_manager.get_block_with_label(agent_id=sarah_agent.id, block_label=default_block.label, actor=default_user)

    assert block.id == default_block.id
    assert block.label == default_block.label


def test_refresh_memory(server: SyncServer, default_user):
    block = server.block_manager.create_or_update_block(
        PydanticBlock(
            label="test",
            value="test",
            limit=1000,
        ),
        actor=default_user,
    )
    agent = server.agent_manager.create_agent(
        CreateAgent(
            name="test",
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )
    assert len(agent.memory.blocks) == 0
    agent = server.agent_manager.refresh_memory(agent_state=agent, actor=default_user)
    assert len(agent.memory.blocks) == 0


# ======================================================================================================================
# Agent Manager - Passages Tests
# ======================================================================================================================


def test_agent_list_passages_basic(server, default_user, sarah_agent, agent_passages_setup):
    """Test basic listing functionality of agent passages"""

    all_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id)
    assert len(all_passages) == 5  # 3 source + 2 agent passages


def test_agent_list_passages_ordering(server, default_user, sarah_agent, agent_passages_setup):
    """Test ordering of agent passages"""

    # Test ascending order
    asc_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, ascending=True)
    assert len(asc_passages) == 5
    for i in range(1, len(asc_passages)):
        assert asc_passages[i - 1].created_at <= asc_passages[i].created_at

    # Test descending order
    desc_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, ascending=False)
    assert len(desc_passages) == 5
    for i in range(1, len(desc_passages)):
        assert desc_passages[i - 1].created_at >= desc_passages[i].created_at


def test_agent_list_passages_pagination(server, default_user, sarah_agent, agent_passages_setup):
    """Test pagination of agent passages"""

    # Test limit
    limited_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, limit=3)
    assert len(limited_passages) == 3

    # Test cursor-based pagination
    first_page = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, limit=2, ascending=True)
    assert len(first_page) == 2

    second_page = server.agent_manager.list_passages(
        actor=default_user, agent_id=sarah_agent.id, after=first_page[-1].id, limit=2, ascending=True
    )
    assert len(second_page) == 2
    assert first_page[-1].id != second_page[0].id
    assert first_page[-1].created_at <= second_page[0].created_at

    """
    [1]   [2]
    * * | * *

       [mid]
    * | * * | *
    """
    middle_page = server.agent_manager.list_passages(
        actor=default_user, agent_id=sarah_agent.id, before=second_page[-1].id, after=first_page[0].id, ascending=True
    )
    assert len(middle_page) == 2
    assert middle_page[0].id == first_page[-1].id
    assert middle_page[1].id == second_page[0].id

    middle_page_desc = server.agent_manager.list_passages(
        actor=default_user, agent_id=sarah_agent.id, before=second_page[-1].id, after=first_page[0].id, ascending=False
    )
    assert len(middle_page_desc) == 2
    assert middle_page_desc[0].id == second_page[0].id
    assert middle_page_desc[1].id == first_page[-1].id


def test_agent_list_passages_text_search(server, default_user, sarah_agent, agent_passages_setup):
    """Test text search functionality of agent passages"""

    # Test text search for source passages
    source_text_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, query_text="Source passage")
    assert len(source_text_passages) == 3

    # Test text search for agent passages
    agent_text_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, query_text="Agent passage")
    assert len(agent_text_passages) == 2


def test_agent_list_passages_agent_only(server, default_user, sarah_agent, agent_passages_setup):
    """Test text search functionality of agent passages"""

    # Test text search for agent passages
    agent_text_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, agent_only=True)
    assert len(agent_text_passages) == 2


def test_agent_list_passages_filtering(server, default_user, sarah_agent, default_source, agent_passages_setup):
    """Test filtering functionality of agent passages"""

    # Test source filtering
    source_filtered = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, source_id=default_source.id)
    assert len(source_filtered) == 3

    # Test date filtering
    now = datetime.now(timezone.utc)
    future_date = now + timedelta(days=1)
    past_date = now - timedelta(days=1)

    date_filtered = server.agent_manager.list_passages(
        actor=default_user, agent_id=sarah_agent.id, start_date=past_date, end_date=future_date
    )
    assert len(date_filtered) == 5


def test_agent_list_passages_vector_search(server, default_user, sarah_agent, default_source):
    """Test vector search functionality of agent passages"""
    embed_model = embedding_model(DEFAULT_EMBEDDING_CONFIG)

    # Create passages with known embeddings
    passages = []

    # Create passages with different embeddings
    test_passages = [
        "I like red",
        "random text",
        "blue shoes",
    ]

    server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id=default_source.id, actor=default_user)

    for i, text in enumerate(test_passages):
        embedding = embed_model.get_text_embedding(text)
        if i % 2 == 0:
            passage = PydanticPassage(
                text=text,
                organization_id=default_user.organization_id,
                agent_id=sarah_agent.id,
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
                embedding=embedding,
            )
        else:
            passage = PydanticPassage(
                text=text,
                organization_id=default_user.organization_id,
                source_id=default_source.id,
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
                embedding=embedding,
            )
        created_passage = server.passage_manager.create_passage(passage, default_user)
        passages.append(created_passage)

    # Query vector similar to "red" embedding
    query_key = "What's my favorite color?"

    # Test vector search with all passages
    results = server.agent_manager.list_passages(
        actor=default_user,
        agent_id=sarah_agent.id,
        query_text=query_key,
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
        embed_query=True,
    )

    # Verify results are ordered by similarity
    assert len(results) == 3
    assert results[0].text == "I like red"
    assert "random" in results[1].text or "random" in results[2].text
    assert "blue" in results[1].text or "blue" in results[2].text

    # Test vector search with agent_only=True
    agent_only_results = server.agent_manager.list_passages(
        actor=default_user,
        agent_id=sarah_agent.id,
        query_text=query_key,
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
        embed_query=True,
        agent_only=True,
    )

    # Verify agent-only results
    assert len(agent_only_results) == 2
    assert agent_only_results[0].text == "I like red"
    assert agent_only_results[1].text == "blue shoes"


def test_list_source_passages_only(server: SyncServer, default_user, default_source, agent_passages_setup):
    """Test listing passages from a source without specifying an agent."""

    # List passages by source_id without agent_id
    source_passages = server.agent_manager.list_passages(
        actor=default_user,
        source_id=default_source.id,
    )

    # Verify we get only source passages (3 from agent_passages_setup)
    assert len(source_passages) == 3
    assert all(p.source_id == default_source.id for p in source_passages)
    assert all(p.agent_id is None for p in source_passages)


# ======================================================================================================================
# Organization Manager Tests
# ======================================================================================================================
def test_list_organizations(server: SyncServer):
    # Create a new org and confirm that it is created correctly
    org_name = "test"
    org = server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name=org_name))

    orgs = server.organization_manager.list_organizations()
    assert len(orgs) == 1
    assert orgs[0].name == org_name

    # Delete it after
    server.organization_manager.delete_organization_by_id(org.id)
    assert len(server.organization_manager.list_organizations()) == 0


def test_create_default_organization(server: SyncServer):
    server.organization_manager.create_default_organization()
    retrieved = server.organization_manager.get_default_organization()
    assert retrieved.name == server.organization_manager.DEFAULT_ORG_NAME


def test_update_organization_name(server: SyncServer):
    org_name_a = "a"
    org_name_b = "b"
    org = server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name=org_name_a))
    assert org.name == org_name_a
    org = server.organization_manager.update_organization_name_using_id(org_id=org.id, name=org_name_b)
    assert org.name == org_name_b


def test_update_organization_privileged_tools(server: SyncServer):
    org_name = "test"
    org = server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name=org_name))
    assert org.privileged_tools == False
    org = server.organization_manager.update_organization(org_id=org.id, org_update=OrganizationUpdate(privileged_tools=True))
    assert org.privileged_tools == True


def test_list_organizations_pagination(server: SyncServer):
    server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name="a"))
    server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name="b"))

    orgs_x = server.organization_manager.list_organizations(limit=1)
    assert len(orgs_x) == 1

    orgs_y = server.organization_manager.list_organizations(after=orgs_x[0].id, limit=1)
    assert len(orgs_y) == 1
    assert orgs_y[0].name != orgs_x[0].name

    orgs = server.organization_manager.list_organizations(after=orgs_y[0].id, limit=1)
    assert len(orgs) == 0


# ======================================================================================================================
# Passage Manager Tests
# ======================================================================================================================


def test_passage_create_agentic(server: SyncServer, agent_passage_fixture, default_user):
    """Test creating a passage using agent_passage_fixture fixture"""
    assert agent_passage_fixture.id is not None
    assert agent_passage_fixture.text == "Hello, I am an agent passage"

    # Verify we can retrieve it
    retrieved = server.passage_manager.get_passage_by_id(
        agent_passage_fixture.id,
        actor=default_user,
    )
    assert retrieved is not None
    assert retrieved.id == agent_passage_fixture.id
    assert retrieved.text == agent_passage_fixture.text


def test_passage_create_source(server: SyncServer, source_passage_fixture, default_user):
    """Test creating a source passage."""
    assert source_passage_fixture is not None
    assert source_passage_fixture.text == "Hello, I am a source passage"

    # Verify we can retrieve it
    retrieved = server.passage_manager.get_passage_by_id(
        source_passage_fixture.id,
        actor=default_user,
    )
    assert retrieved is not None
    assert retrieved.id == source_passage_fixture.id
    assert retrieved.text == source_passage_fixture.text


def test_passage_create_invalid(server: SyncServer, agent_passage_fixture, default_user):
    """Test creating an agent passage."""
    assert agent_passage_fixture is not None
    assert agent_passage_fixture.text == "Hello, I am an agent passage"

    # Try to create an invalid passage (with both agent_id and source_id)
    with pytest.raises(AssertionError):
        server.passage_manager.create_passage(
            PydanticPassage(
                text="Invalid passage",
                agent_id="123",
                source_id="456",
                organization_id=default_user.organization_id,
                embedding=[0.1] * 1024,
                embedding_config=DEFAULT_EMBEDDING_CONFIG,
            ),
            actor=default_user,
        )


def test_passage_get_by_id(server: SyncServer, agent_passage_fixture, source_passage_fixture, default_user):
    """Test retrieving a passage by ID"""
    retrieved = server.passage_manager.get_passage_by_id(agent_passage_fixture.id, actor=default_user)
    assert retrieved is not None
    assert retrieved.id == agent_passage_fixture.id
    assert retrieved.text == agent_passage_fixture.text

    retrieved = server.passage_manager.get_passage_by_id(source_passage_fixture.id, actor=default_user)
    assert retrieved is not None
    assert retrieved.id == source_passage_fixture.id
    assert retrieved.text == source_passage_fixture.text


def test_passage_cascade_deletion(
    server: SyncServer, agent_passage_fixture, source_passage_fixture, default_user, default_source, sarah_agent
):
    """Test that passages are deleted when their parent (agent or source) is deleted."""
    # Verify passages exist
    agent_passage = server.passage_manager.get_passage_by_id(agent_passage_fixture.id, default_user)
    source_passage = server.passage_manager.get_passage_by_id(source_passage_fixture.id, default_user)
    assert agent_passage is not None
    assert source_passage is not None

    # Delete agent and verify its passages are deleted
    server.agent_manager.delete_agent(sarah_agent.id, default_user)
    agentic_passages = server.agent_manager.list_passages(actor=default_user, agent_id=sarah_agent.id, agent_only=True)
    assert len(agentic_passages) == 0

    # Delete source and verify its passages are deleted
    server.source_manager.delete_source(default_source.id, default_user)
    with pytest.raises(NoResultFound):
        server.passage_manager.get_passage_by_id(source_passage_fixture.id, default_user)


# ======================================================================================================================
# User Manager Tests
# ======================================================================================================================
def test_list_users(server: SyncServer):
    # Create default organization
    org = server.organization_manager.create_default_organization()

    user_name = "user"
    user = server.user_manager.create_user(PydanticUser(name=user_name, organization_id=org.id))

    users = server.user_manager.list_users()
    assert len(users) == 1
    assert users[0].name == user_name

    # Delete it after
    server.user_manager.delete_user_by_id(user.id)
    assert len(server.user_manager.list_users()) == 0


def test_create_default_user(server: SyncServer):
    org = server.organization_manager.create_default_organization()
    server.user_manager.create_default_user(org_id=org.id)
    retrieved = server.user_manager.get_default_user()
    assert retrieved.name == server.user_manager.DEFAULT_USER_NAME


def test_update_user(server: SyncServer):
    # Create default organization
    default_org = server.organization_manager.create_default_organization()
    test_org = server.organization_manager.create_organization(PydanticOrganization(name="test_org"))

    user_name_a = "a"
    user_name_b = "b"

    # Assert it's been created
    user = server.user_manager.create_user(PydanticUser(name=user_name_a, organization_id=default_org.id))
    assert user.name == user_name_a

    # Adjust name
    user = server.user_manager.update_user(UserUpdate(id=user.id, name=user_name_b))
    assert user.name == user_name_b
    assert user.organization_id == OrganizationManager.DEFAULT_ORG_ID

    # Adjust org id
    user = server.user_manager.update_user(UserUpdate(id=user.id, organization_id=test_org.id))
    assert user.name == user_name_b
    assert user.organization_id == test_org.id


# ======================================================================================================================
# ToolManager Tests
# ======================================================================================================================


def test_create_tool(server: SyncServer, print_tool, default_user, default_organization):
    # Assertions to ensure the created tool matches the expected values
    assert print_tool.created_by_id == default_user.id
    assert print_tool.organization_id == default_organization.id
    assert print_tool.tool_type == ToolType.CUSTOM


def test_create_composio_tool(server: SyncServer, composio_github_star_tool, default_user, default_organization):
    # Assertions to ensure the created tool matches the expected values
    assert composio_github_star_tool.created_by_id == default_user.id
    assert composio_github_star_tool.organization_id == default_organization.id
    assert composio_github_star_tool.tool_type == ToolType.EXTERNAL_COMPOSIO


def test_create_mcp_tool(server: SyncServer, mcp_tool, default_user, default_organization):
    # Assertions to ensure the created tool matches the expected values
    assert mcp_tool.created_by_id == default_user.id
    assert mcp_tool.organization_id == default_organization.id
    assert mcp_tool.tool_type == ToolType.EXTERNAL_MCP
    assert mcp_tool.metadata_[MCP_TOOL_TAG_NAME_PREFIX]["server_name"] == "test"


@pytest.mark.skipif(USING_SQLITE, reason="Test not applicable when using SQLite.")
def test_create_tool_duplicate_name(server: SyncServer, print_tool, default_user, default_organization):
    data = print_tool.model_dump(exclude=["id"])
    tool = PydanticTool(**data)

    with pytest.raises(UniqueConstraintViolationError):
        server.tool_manager.create_tool(tool, actor=default_user)


def test_get_tool_by_id(server: SyncServer, print_tool, default_user):
    # Fetch the tool by ID using the manager method
    fetched_tool = server.tool_manager.get_tool_by_id(print_tool.id, actor=default_user)

    # Assertions to check if the fetched tool matches the created tool
    assert fetched_tool.id == print_tool.id
    assert fetched_tool.name == print_tool.name
    assert fetched_tool.description == print_tool.description
    assert fetched_tool.tags == print_tool.tags
    assert fetched_tool.metadata_ == print_tool.metadata_
    assert fetched_tool.source_code == print_tool.source_code
    assert fetched_tool.source_type == print_tool.source_type
    assert fetched_tool.tool_type == ToolType.CUSTOM


def test_get_tool_with_actor(server: SyncServer, print_tool, default_user):
    # Fetch the print_tool by name and organization ID
    fetched_tool = server.tool_manager.get_tool_by_name(print_tool.name, actor=default_user)

    # Assertions to check if the fetched tool matches the created tool
    assert fetched_tool.id == print_tool.id
    assert fetched_tool.name == print_tool.name
    assert fetched_tool.created_by_id == default_user.id
    assert fetched_tool.description == print_tool.description
    assert fetched_tool.tags == print_tool.tags
    assert fetched_tool.source_code == print_tool.source_code
    assert fetched_tool.source_type == print_tool.source_type
    assert fetched_tool.tool_type == ToolType.CUSTOM


def test_list_tools(server: SyncServer, print_tool, default_user):
    # List tools (should include the one created by the fixture)
    tools = server.tool_manager.list_tools(actor=default_user)

    # Assertions to check that the created tool is listed
    assert len(tools) == 1
    assert any(t.id == print_tool.id for t in tools)


def test_update_tool_by_id(server: SyncServer, print_tool, default_user):
    updated_description = "updated_description"
    return_char_limit = 10000

    # Create a ToolUpdate object to modify the print_tool's description
    tool_update = ToolUpdate(description=updated_description, return_char_limit=return_char_limit)

    # Update the tool using the manager method
    server.tool_manager.update_tool_by_id(print_tool.id, tool_update, actor=default_user)

    # Fetch the updated tool to verify the changes
    updated_tool = server.tool_manager.get_tool_by_id(print_tool.id, actor=default_user)

    # Assertions to check if the update was successful
    assert updated_tool.description == updated_description
    assert updated_tool.return_char_limit == return_char_limit


def test_update_tool_source_code_refreshes_schema_and_name(server: SyncServer, print_tool, default_user):
    def counter_tool(counter: int):
        """
        Args:
            counter (int): The counter to count to.

        Returns:
            bool: If it successfully counted to the counter.
        """
        for c in range(counter):
            print(c)

        return True

    # Test begins
    og_json_schema = print_tool.json_schema

    source_code = parse_source_code(counter_tool)

    # Create a ToolUpdate object to modify the tool's source_code
    tool_update = ToolUpdate(source_code=source_code)

    # Update the tool using the manager method
    server.tool_manager.update_tool_by_id(print_tool.id, tool_update, actor=default_user)

    # Fetch the updated tool to verify the changes
    updated_tool = server.tool_manager.get_tool_by_id(print_tool.id, actor=default_user)

    # Assertions to check if the update was successful, and json_schema is updated as well
    assert updated_tool.source_code == source_code
    assert updated_tool.json_schema != og_json_schema

    new_schema = derive_openai_json_schema(source_code=updated_tool.source_code)
    assert updated_tool.json_schema == new_schema
    assert updated_tool.tool_type == ToolType.CUSTOM


def test_update_tool_source_code_refreshes_schema_only(server: SyncServer, print_tool, default_user):
    def counter_tool(counter: int):
        """
        Args:
            counter (int): The counter to count to.

        Returns:
            bool: If it successfully counted to the counter.
        """
        for c in range(counter):
            print(c)

        return True

    # Test begins
    og_json_schema = print_tool.json_schema

    source_code = parse_source_code(counter_tool)
    name = "counter_tool"

    # Create a ToolUpdate object to modify the tool's source_code
    tool_update = ToolUpdate(source_code=source_code)

    # Update the tool using the manager method
    server.tool_manager.update_tool_by_id(print_tool.id, tool_update, actor=default_user)

    # Fetch the updated tool to verify the changes
    updated_tool = server.tool_manager.get_tool_by_id(print_tool.id, actor=default_user)

    # Assertions to check if the update was successful, and json_schema is updated as well
    assert updated_tool.source_code == source_code
    assert updated_tool.json_schema != og_json_schema

    new_schema = derive_openai_json_schema(source_code=updated_tool.source_code, name=updated_tool.name)
    assert updated_tool.json_schema == new_schema
    assert updated_tool.name == name
    assert updated_tool.tool_type == ToolType.CUSTOM


def test_update_tool_multi_user(server: SyncServer, print_tool, default_user, other_user):
    updated_description = "updated_description"

    # Create a ToolUpdate object to modify the print_tool's description
    tool_update = ToolUpdate(description=updated_description)

    # Update the print_tool using the manager method, but WITH THE OTHER USER'S ID!
    server.tool_manager.update_tool_by_id(print_tool.id, tool_update, actor=other_user)

    # Check that the created_by and last_updated_by fields are correct
    # Fetch the updated print_tool to verify the changes
    updated_tool = server.tool_manager.get_tool_by_id(print_tool.id, actor=default_user)

    assert updated_tool.last_updated_by_id == other_user.id
    assert updated_tool.created_by_id == default_user.id


def test_delete_tool_by_id(server: SyncServer, print_tool, default_user):
    # Delete the print_tool using the manager method
    server.tool_manager.delete_tool_by_id(print_tool.id, actor=default_user)

    tools = server.tool_manager.list_tools(actor=default_user)
    assert len(tools) == 0


def test_upsert_base_tools(server: SyncServer, default_user):
    tools = server.tool_manager.upsert_base_tools(actor=default_user)
    expected_tool_names = sorted(set(BASE_TOOLS + BASE_MEMORY_TOOLS + MULTI_AGENT_TOOLS + BASE_SLEEPTIME_TOOLS))
    assert sorted([t.name for t in tools]) == expected_tool_names

    # Call it again to make sure it doesn't create duplicates
    tools = server.tool_manager.upsert_base_tools(actor=default_user)
    assert sorted([t.name for t in tools]) == expected_tool_names

    # Confirm that the return tools have no source_code, but a json_schema
    for t in tools:
        if t.name in BASE_TOOLS:
            assert t.tool_type == ToolType.LETTA_CORE
        elif t.name in BASE_MEMORY_TOOLS:
            assert t.tool_type == ToolType.LETTA_MEMORY_CORE
        elif t.name in MULTI_AGENT_TOOLS:
            assert t.tool_type == ToolType.LETTA_MULTI_AGENT_CORE
        elif t.name in BASE_SLEEPTIME_TOOLS:
            assert t.tool_type == ToolType.LETTA_SLEEPTIME_CORE
        else:
            pytest.fail(f"The tool name is unrecognized as a base tool: {t.name}")
        assert t.source_code is None
        assert t.json_schema


# ======================================================================================================================
# Message Manager Tests
# ======================================================================================================================


def test_message_create(server: SyncServer, hello_world_message_fixture, default_user):
    """Test creating a message using hello_world_message_fixture fixture"""
    assert hello_world_message_fixture.id is not None
    assert hello_world_message_fixture.content[0].text == "Hello, world!"
    assert hello_world_message_fixture.role == "user"

    # Verify we can retrieve it
    retrieved = server.message_manager.get_message_by_id(
        hello_world_message_fixture.id,
        actor=default_user,
    )
    assert retrieved is not None
    assert retrieved.id == hello_world_message_fixture.id
    assert retrieved.content[0].text == hello_world_message_fixture.content[0].text
    assert retrieved.role == hello_world_message_fixture.role


def test_message_get_by_id(server: SyncServer, hello_world_message_fixture, default_user):
    """Test retrieving a message by ID"""
    retrieved = server.message_manager.get_message_by_id(hello_world_message_fixture.id, actor=default_user)
    assert retrieved is not None
    assert retrieved.id == hello_world_message_fixture.id
    assert retrieved.content[0].text == hello_world_message_fixture.content[0].text


def test_message_update(server: SyncServer, hello_world_message_fixture, default_user, other_user):
    """Test updating a message"""
    new_text = "Updated text"
    updated = server.message_manager.update_message_by_id(hello_world_message_fixture.id, MessageUpdate(content=new_text), actor=other_user)
    assert updated is not None
    assert updated.content[0].text == new_text
    retrieved = server.message_manager.get_message_by_id(hello_world_message_fixture.id, actor=default_user)
    assert retrieved.content[0].text == new_text

    # Assert that orm metadata fields are populated
    assert retrieved.created_by_id == default_user.id
    assert retrieved.last_updated_by_id == other_user.id


def test_message_delete(server: SyncServer, hello_world_message_fixture, default_user):
    """Test deleting a message"""
    server.message_manager.delete_message_by_id(hello_world_message_fixture.id, actor=default_user)
    retrieved = server.message_manager.get_message_by_id(hello_world_message_fixture.id, actor=default_user)
    assert retrieved is None


def test_message_size(server: SyncServer, hello_world_message_fixture, default_user):
    """Test counting messages with filters"""
    base_message = hello_world_message_fixture

    # Create additional test messages
    messages = [
        PydanticMessage(
            organization_id=default_user.organization_id,
            agent_id=base_message.agent_id,
            role=base_message.role,
            content=[TextContent(text=f"Test message {i}")],
        )
        for i in range(4)
    ]
    server.message_manager.create_many_messages(messages, actor=default_user)

    # Test total count
    total = server.message_manager.size(actor=default_user, role=MessageRole.user)
    assert total == 6  # login message + base message + 4 test messages
    # TODO: change login message to be a system not user message

    # Test count with agent filter
    agent_count = server.message_manager.size(actor=default_user, agent_id=base_message.agent_id, role=MessageRole.user)
    assert agent_count == 6

    # Test count with role filter
    role_count = server.message_manager.size(actor=default_user, role=base_message.role)
    assert role_count == 6

    # Test count with non-existent filter
    empty_count = server.message_manager.size(actor=default_user, agent_id="non-existent", role=MessageRole.user)
    assert empty_count == 0


def create_test_messages(server: SyncServer, base_message: PydanticMessage, default_user) -> list[PydanticMessage]:
    """Helper function to create test messages for all tests"""
    messages = [
        PydanticMessage(
            organization_id=default_user.organization_id,
            agent_id=base_message.agent_id,
            role=base_message.role,
            content=[TextContent(text=f"Test message {i}")],
        )
        for i in range(4)
    ]
    server.message_manager.create_many_messages(messages, actor=default_user)
    return messages


def test_get_messages_by_ids(server: SyncServer, hello_world_message_fixture, default_user, sarah_agent):
    """Test basic message listing with limit"""
    messages = create_test_messages(server, hello_world_message_fixture, default_user)
    message_ids = [m.id for m in messages]

    results = server.message_manager.get_messages_by_ids(message_ids=message_ids, actor=default_user)
    assert sorted(message_ids) == sorted([r.id for r in results])


def test_message_listing_basic(server: SyncServer, hello_world_message_fixture, default_user, sarah_agent):
    """Test basic message listing with limit"""
    create_test_messages(server, hello_world_message_fixture, default_user)

    results = server.message_manager.list_user_messages_for_agent(agent_id=sarah_agent.id, limit=3, actor=default_user)
    assert len(results) == 3


def test_message_listing_cursor(server: SyncServer, hello_world_message_fixture, default_user, sarah_agent):
    """Test cursor-based pagination functionality"""
    create_test_messages(server, hello_world_message_fixture, default_user)

    # Make sure there are 6 messages
    assert server.message_manager.size(actor=default_user, role=MessageRole.user) == 6

    # Get first page
    first_page = server.message_manager.list_user_messages_for_agent(agent_id=sarah_agent.id, actor=default_user, limit=3)
    assert len(first_page) == 3

    last_id_on_first_page = first_page[-1].id

    # Get second page
    second_page = server.message_manager.list_user_messages_for_agent(
        agent_id=sarah_agent.id, actor=default_user, after=last_id_on_first_page, limit=3
    )
    assert len(second_page) == 3  # Should have 3 remaining messages
    assert all(r1.id != r2.id for r1 in first_page for r2 in second_page)

    # Get the middle
    middle_page = server.message_manager.list_user_messages_for_agent(
        agent_id=sarah_agent.id, actor=default_user, before=second_page[1].id, after=first_page[0].id
    )
    assert len(middle_page) == 3
    assert middle_page[0].id == first_page[1].id
    assert middle_page[1].id == first_page[-1].id
    assert middle_page[-1].id == second_page[0].id

    middle_page_desc = server.message_manager.list_user_messages_for_agent(
        agent_id=sarah_agent.id, actor=default_user, before=second_page[1].id, after=first_page[0].id, ascending=False
    )
    assert len(middle_page_desc) == 3
    assert middle_page_desc[0].id == second_page[0].id
    assert middle_page_desc[1].id == first_page[-1].id
    assert middle_page_desc[-1].id == first_page[1].id


def test_message_listing_filtering(server: SyncServer, hello_world_message_fixture, default_user, sarah_agent):
    """Test filtering messages by agent ID"""
    create_test_messages(server, hello_world_message_fixture, default_user)

    agent_results = server.message_manager.list_user_messages_for_agent(agent_id=sarah_agent.id, actor=default_user, limit=10)
    assert len(agent_results) == 6  # login message + base message + 4 test messages
    assert all(msg.agent_id == hello_world_message_fixture.agent_id for msg in agent_results)


def test_message_listing_text_search(server: SyncServer, hello_world_message_fixture, default_user, sarah_agent):
    """Test searching messages by text content"""
    create_test_messages(server, hello_world_message_fixture, default_user)

    search_results = server.message_manager.list_user_messages_for_agent(
        agent_id=sarah_agent.id, actor=default_user, query_text="Test message", limit=10
    )
    assert len(search_results) == 4
    assert all("Test message" in msg.content[0].text for msg in search_results)

    # Test no results
    search_results = server.message_manager.list_user_messages_for_agent(
        agent_id=sarah_agent.id, actor=default_user, query_text="Letta", limit=10
    )
    assert len(search_results) == 0


# ======================================================================================================================
# Block Manager Tests - Basic
# ======================================================================================================================


def test_create_block(server: SyncServer, default_user):
    block_manager = BlockManager()
    block_create = PydanticBlock(
        label="human",
        is_template=True,
        value="Sample content",
        template_name="sample_template",
        description="A test block",
        limit=1000,
        metadata={"example": "data"},
    )

    block = block_manager.create_or_update_block(block_create, actor=default_user)

    # Assertions to ensure the created block matches the expected values
    assert block.label == block_create.label
    assert block.is_template == block_create.is_template
    assert block.value == block_create.value
    assert block.template_name == block_create.template_name
    assert block.description == block_create.description
    assert block.limit == block_create.limit
    assert block.metadata == block_create.metadata
    assert block.organization_id == default_user.organization_id


def test_get_blocks(server, default_user):
    block_manager = BlockManager()

    # Create blocks to retrieve later
    block_manager.create_or_update_block(PydanticBlock(label="human", value="Block 1"), actor=default_user)
    block_manager.create_or_update_block(PydanticBlock(label="persona", value="Block 2"), actor=default_user)

    # Retrieve blocks by different filters
    all_blocks = block_manager.get_blocks(actor=default_user)
    assert len(all_blocks) == 2

    human_blocks = block_manager.get_blocks(actor=default_user, label="human")
    assert len(human_blocks) == 1
    assert human_blocks[0].label == "human"

    persona_blocks = block_manager.get_blocks(actor=default_user, label="persona")
    assert len(persona_blocks) == 1
    assert persona_blocks[0].label == "persona"


def test_get_blocks_comprehensive(server, default_user, other_user_different_org):
    def random_label(prefix="label"):
        return f"{prefix}_{''.join(random.choices(string.ascii_lowercase, k=6))}"

    def random_value():
        return "".join(random.choices(string.ascii_letters + string.digits, k=12))

    block_manager = BlockManager()

    # Create 10 blocks for default_user
    default_user_blocks = []
    for _ in range(10):
        label = random_label("default")
        value = random_value()
        block_manager.create_or_update_block(PydanticBlock(label=label, value=value), actor=default_user)
        default_user_blocks.append((label, value))

    # Create 3 blocks for other_user
    other_user_blocks = []
    for _ in range(3):
        label = random_label("other")
        value = random_value()
        block_manager.create_or_update_block(PydanticBlock(label=label, value=value), actor=other_user_different_org)
        other_user_blocks.append((label, value))

    # Check default_user sees only their blocks
    retrieved_default_blocks = block_manager.get_blocks(actor=default_user)
    assert len(retrieved_default_blocks) == 10
    retrieved_labels = {b.label for b in retrieved_default_blocks}
    for label, value in default_user_blocks:
        assert label in retrieved_labels

    # Check individual filtering for default_user
    for label, value in default_user_blocks:
        filtered = block_manager.get_blocks(actor=default_user, label=label)
        assert len(filtered) == 1
        assert filtered[0].label == label
        assert filtered[0].value == value

    # Check other_user sees only their blocks
    retrieved_other_blocks = block_manager.get_blocks(actor=other_user_different_org)
    assert len(retrieved_other_blocks) == 3
    retrieved_labels = {b.label for b in retrieved_other_blocks}
    for label, value in other_user_blocks:
        assert label in retrieved_labels

    # Other user shouldn't see default_user's blocks
    for label, _ in default_user_blocks:
        assert block_manager.get_blocks(actor=other_user_different_org, label=label) == []

    # Default user shouldn't see other_user's blocks
    for label, _ in other_user_blocks:
        assert block_manager.get_blocks(actor=default_user, label=label) == []


def test_update_block(server: SyncServer, default_user):
    block_manager = BlockManager()
    block = block_manager.create_or_update_block(PydanticBlock(label="persona", value="Original Content"), actor=default_user)

    # Update block's content
    update_data = BlockUpdate(value="Updated Content", description="Updated description")
    block_manager.update_block(block_id=block.id, block_update=update_data, actor=default_user)

    # Retrieve the updated block
    updated_block = block_manager.get_blocks(actor=default_user, id=block.id)[0]

    # Assertions to verify the update
    assert updated_block.value == "Updated Content"
    assert updated_block.description == "Updated description"


def test_update_block_limit(server: SyncServer, default_user):
    block_manager = BlockManager()
    block = block_manager.create_or_update_block(PydanticBlock(label="persona", value="Original Content"), actor=default_user)

    limit = len("Updated Content") * 2000
    update_data = BlockUpdate(value="Updated Content" * 2000, description="Updated description")

    # Check that exceeding the block limit raises an exception
    with pytest.raises(ValueError):
        block_manager.update_block(block_id=block.id, block_update=update_data, actor=default_user)

    # Ensure the update works when within limits
    update_data = BlockUpdate(value="Updated Content" * 2000, description="Updated description", limit=limit)
    block_manager.update_block(block_id=block.id, block_update=update_data, actor=default_user)

    # Retrieve the updated block and validate the update
    updated_block = block_manager.get_blocks(actor=default_user, id=block.id)[0]

    assert updated_block.value == "Updated Content" * 2000
    assert updated_block.description == "Updated description"


def test_update_block_limit_does_not_reset(server: SyncServer, default_user):
    block_manager = BlockManager()
    new_content = "Updated Content" * 2000
    limit = len(new_content)
    block = block_manager.create_or_update_block(PydanticBlock(label="persona", value="Original Content", limit=limit), actor=default_user)

    # Ensure the update works
    update_data = BlockUpdate(value=new_content)
    block_manager.update_block(block_id=block.id, block_update=update_data, actor=default_user)

    # Retrieve the updated block and validate the update
    updated_block = block_manager.get_blocks(actor=default_user, id=block.id)[0]
    assert updated_block.value == new_content


def test_delete_block(server: SyncServer, default_user):
    block_manager = BlockManager()

    # Create and delete a block
    block = block_manager.create_or_update_block(PydanticBlock(label="human", value="Sample content"), actor=default_user)
    block_manager.delete_block(block_id=block.id, actor=default_user)

    # Verify that the block was deleted
    blocks = block_manager.get_blocks(actor=default_user)
    assert len(blocks) == 0


def test_delete_block_detaches_from_agent(server: SyncServer, sarah_agent, default_user):
    # Create and delete a block
    block = server.block_manager.create_or_update_block(PydanticBlock(label="human", value="Sample content"), actor=default_user)
    agent_state = server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=block.id, actor=default_user)

    # Check that block has been attached
    assert block.id in [b.id for b in agent_state.memory.blocks]

    # Now attempt to delete the block
    server.block_manager.delete_block(block_id=block.id, actor=default_user)

    # Verify that the block was deleted
    blocks = server.block_manager.get_blocks(actor=default_user)
    assert len(blocks) == 0

    # Check that block has been detached too
    agent_state = server.agent_manager.get_agent_by_id(agent_id=sarah_agent.id, actor=default_user)
    assert not (block.id in [b.id for b in agent_state.memory.blocks])


def test_get_agents_for_block(server: SyncServer, sarah_agent, charles_agent, default_user):
    # Create and delete a block
    block = server.block_manager.create_or_update_block(PydanticBlock(label="alien", value="Sample content"), actor=default_user)
    sarah_agent = server.agent_manager.attach_block(agent_id=sarah_agent.id, block_id=block.id, actor=default_user)
    charles_agent = server.agent_manager.attach_block(agent_id=charles_agent.id, block_id=block.id, actor=default_user)

    # Check that block has been attached to both
    assert block.id in [b.id for b in sarah_agent.memory.blocks]
    assert block.id in [b.id for b in charles_agent.memory.blocks]

    # Get the agents for that block
    agent_states = server.block_manager.get_agents_for_block(block_id=block.id, actor=default_user)
    assert len(agent_states) == 2

    # Check both agents are in the list
    agent_state_ids = [a.id for a in agent_states]
    assert sarah_agent.id in agent_state_ids
    assert charles_agent.id in agent_state_ids


# ======================================================================================================================
# Block Manager Tests - Checkpointing
# ======================================================================================================================


def test_checkpoint_creates_history(server: SyncServer, default_user):
    """
    Ensures that calling checkpoint_block creates a BlockHistory row and updates
    the block's current_history_entry_id appropriately.
    """

    block_manager = BlockManager()

    # Create a block
    initial_value = "Initial block content"
    created_block = block_manager.create_or_update_block(PydanticBlock(label="test_checkpoint", value=initial_value), actor=default_user)

    # Act: checkpoint it
    block_manager.checkpoint_block(block_id=created_block.id, actor=default_user)

    with db_context() as session:
        # Get BlockHistory entries for this block
        history_entries: List[BlockHistory] = session.query(BlockHistory).filter(BlockHistory.block_id == created_block.id).all()
        assert len(history_entries) == 1, "Exactly one history entry should be created"
        hist = history_entries[0]

        # Fetch ORM block for internal checks
        db_block = session.get(Block, created_block.id)

        assert hist.sequence_number == 1
        assert hist.value == initial_value
        assert hist.actor_type == ActorType.LETTA_USER
        assert hist.actor_id == default_user.id
        assert db_block.current_history_entry_id == hist.id


def test_multiple_checkpoints(server: SyncServer, default_user):
    block_manager = BlockManager()

    # Create a block
    block = block_manager.create_or_update_block(PydanticBlock(label="test_multi_checkpoint", value="v1"), actor=default_user)

    # 1) First checkpoint
    block_manager.checkpoint_block(block_id=block.id, actor=default_user)

    # 2) Update block content
    updated_block_data = PydanticBlock(**block.model_dump())
    updated_block_data.value = "v2"
    block_manager.create_or_update_block(updated_block_data, actor=default_user)

    # 3) Second checkpoint
    block_manager.checkpoint_block(block_id=block.id, actor=default_user)

    with db_context() as session:
        history_entries = (
            session.query(BlockHistory).filter(BlockHistory.block_id == block.id).order_by(BlockHistory.sequence_number.asc()).all()
        )
        assert len(history_entries) == 2, "Should have two history entries"

        # First is seq=1, value='v1'
        assert history_entries[0].sequence_number == 1
        assert history_entries[0].value == "v1"

        # Second is seq=2, value='v2'
        assert history_entries[1].sequence_number == 2
        assert history_entries[1].value == "v2"

        # The block should now point to the second entry
        db_block = session.get(Block, block.id)
        assert db_block.current_history_entry_id == history_entries[1].id


def test_checkpoint_with_agent_id(server: SyncServer, default_user, sarah_agent):
    """
    Ensures that if we pass agent_id to checkpoint_block, we get
    actor_type=LETTA_AGENT, actor_id=<agent.id> in BlockHistory.
    """
    block_manager = BlockManager()

    # Create a block
    block = block_manager.create_or_update_block(PydanticBlock(label="test_agent_checkpoint", value="Agent content"), actor=default_user)

    # Checkpoint with agent_id
    block_manager.checkpoint_block(block_id=block.id, actor=default_user, agent_id=sarah_agent.id)

    # Verify
    with db_context() as session:
        hist_entry = session.query(BlockHistory).filter(BlockHistory.block_id == block.id).one()
        assert hist_entry.actor_type == ActorType.LETTA_AGENT
        assert hist_entry.actor_id == sarah_agent.id


def test_checkpoint_with_no_state_change(server: SyncServer, default_user):
    """
    If we call checkpoint_block twice without any edits,
    we expect two entries or only one, depending on your policy.
    """
    block_manager = BlockManager()

    # Create block
    block = block_manager.create_or_update_block(PydanticBlock(label="test_no_change", value="original"), actor=default_user)

    # 1) checkpoint
    block_manager.checkpoint_block(block_id=block.id, actor=default_user)
    # 2) checkpoint again (no changes)
    block_manager.checkpoint_block(block_id=block.id, actor=default_user)

    with db_context() as session:
        all_hist = session.query(BlockHistory).filter(BlockHistory.block_id == block.id).all()
        assert len(all_hist) == 2


def test_checkpoint_concurrency_stale(server: SyncServer, default_user):
    block_manager = BlockManager()

    # create block
    block = block_manager.create_or_update_block(PydanticBlock(label="test_stale_checkpoint", value="hello"), actor=default_user)

    # session1 loads
    with db_context() as s1:
        block_s1 = s1.get(Block, block.id)  # version=1

    # session2 loads
    with db_context() as s2:
        block_s2 = s2.get(Block, block.id)  # also version=1

    # session1 checkpoint => version=2
    with db_context() as s1:
        block_s1 = s1.merge(block_s1)
        block_manager.checkpoint_block(
            block_id=block_s1.id,
            actor=default_user,
            use_preloaded_block=block_s1,  # let manager use the object in memory
        )
        # commits inside checkpoint_block => version goes to 2

    # session2 tries to checkpoint => sees old version=1 => stale error
    with pytest.raises(StaleDataError):
        with db_context() as s2:
            block_s2 = s2.merge(block_s2)
            block_manager.checkpoint_block(
                block_id=block_s2.id,
                actor=default_user,
                use_preloaded_block=block_s2,
            )


def test_checkpoint_no_future_states(server: SyncServer, default_user):
    """
    Ensures that if the block is already at the highest sequence,
    creating a new checkpoint does NOT delete anything.
    """

    block_manager = BlockManager()

    # 1) Create block with "v1" and checkpoint => seq=1
    block_v1 = block_manager.create_or_update_block(PydanticBlock(label="no_future_test", value="v1"), actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # 2) Create "v2" and checkpoint => seq=2
    updated_data = PydanticBlock(**block_v1.model_dump())
    updated_data.value = "v2"
    block_manager.create_or_update_block(updated_data, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # So we have seq=1: v1, seq=2: v2. No "future" states.
    # 3) Another checkpoint (no changes made) => should become seq=3, not delete anything
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    with db_context() as session:
        # We expect 3 rows in block_history, none removed
        history_rows = (
            session.query(BlockHistory).filter(BlockHistory.block_id == block_v1.id).order_by(BlockHistory.sequence_number.asc()).all()
        )
        # Should be seq=1, seq=2, seq=3
        assert len(history_rows) == 3
        assert history_rows[0].value == "v1"
        assert history_rows[1].value == "v2"
        # The last is also "v2" if we didn't change it, or the same current fields
        assert history_rows[2].sequence_number == 3
        # There's no leftover row that was deleted


# ======================================================================================================================
# Block Manager Tests - Undo
# ======================================================================================================================


def test_undo_checkpoint_block(server: SyncServer, default_user):
    """
    Verifies that we can undo to the previous checkpoint:
      1) Create a block and checkpoint -> sequence_number=1
      2) Update block content and checkpoint -> sequence_number=2
      3) Undo -> should revert block to sequence_number=1's content
    """
    block_manager = BlockManager()

    # 1) Create block
    initial_value = "Version 1 content"
    created_block = block_manager.create_or_update_block(PydanticBlock(label="undo_test", value=initial_value), actor=default_user)

    # 2) First checkpoint => seq=1
    block_manager.checkpoint_block(block_id=created_block.id, actor=default_user)

    # 3) Update block content to "Version 2"
    updated_data = PydanticBlock(**created_block.model_dump())
    updated_data.value = "Version 2 content"
    block_manager.create_or_update_block(updated_data, actor=default_user)

    # 4) Second checkpoint => seq=2
    block_manager.checkpoint_block(block_id=created_block.id, actor=default_user)

    # 5) Undo => revert to seq=1
    undone_block = block_manager.undo_checkpoint_block(block_id=created_block.id, actor=default_user)

    # 6) Verify the block is now restored to "Version 1" content
    assert undone_block.value == initial_value, "Block should revert to version 1 content"
    assert undone_block.label == "undo_test", "Label should also revert if changed (or remain the same if unchanged)"


def test_checkpoint_deletes_future_states_after_undo(server: SyncServer, default_user):
    """
    Verifies that once we've undone to an earlier checkpoint, creating a new
    checkpoint removes any leftover 'future' states that existed beyond that sequence.
    """
    block_manager = BlockManager()

    # 1) Create block
    block_init = PydanticBlock(label="test_truncation", value="v1")
    block_v1 = block_manager.create_or_update_block(block_init, actor=default_user)
    # Checkpoint => seq=1
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # 2) Update to "v2", checkpoint => seq=2
    block_v2 = PydanticBlock(**block_v1.model_dump())
    block_v2.value = "v2"
    block_manager.create_or_update_block(block_v2, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # 3) Update to "v3", checkpoint => seq=3
    block_v3 = PydanticBlock(**block_v1.model_dump())
    block_v3.value = "v3"
    block_manager.create_or_update_block(block_v3, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # We now have three states in history: seq=1 (v1), seq=2 (v2), seq=3 (v3).

    # Undo from seq=3 -> seq=2
    block_undo_1 = block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)
    assert block_undo_1.value == "v2"

    # Undo from seq=2 -> seq=1
    block_undo_2 = block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)
    assert block_undo_2.value == "v1"

    # 4) Now we are at seq=1. If we checkpoint again, we should remove the old seq=2,3
    #    because the new code truncates future states beyond seq=1.

    # Let's do a new edit: "v1.5"
    block_v1_5 = PydanticBlock(**block_undo_2.model_dump())
    block_v1_5.value = "v1.5"
    block_manager.create_or_update_block(block_v1_5, actor=default_user)

    # 5) Checkpoint => new seq=2, removing the old seq=2 and seq=3
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    with db_context() as session:
        # Let's see which BlockHistory rows remain
        history_entries = (
            session.query(BlockHistory).filter(BlockHistory.block_id == block_v1.id).order_by(BlockHistory.sequence_number.asc()).all()
        )

        # We expect two rows: seq=1 => "v1", seq=2 => "v1.5"
        assert len(history_entries) == 2, f"Expected 2 entries, got {len(history_entries)}"
        assert history_entries[0].sequence_number == 1
        assert history_entries[0].value == "v1"
        assert history_entries[1].sequence_number == 2
        assert history_entries[1].value == "v1.5"

        # No row should contain "v2" or "v3"
        existing_values = {h.value for h in history_entries}
        assert "v2" not in existing_values, "Old seq=2 should have been removed."
        assert "v3" not in existing_values, "Old seq=3 should have been removed."


def test_undo_no_history(server: SyncServer, default_user):
    """
    If a block has never been checkpointed (no current_history_entry_id),
    undo_checkpoint_block should raise a ValueError.
    """
    block_manager = BlockManager()

    # Create a block but don't checkpoint it
    block = block_manager.create_or_update_block(PydanticBlock(label="no_history_test", value="initial"), actor=default_user)

    # Attempt to undo
    with pytest.raises(ValueError, match="has no history entry - cannot undo"):
        block_manager.undo_checkpoint_block(block_id=block.id, actor=default_user)


def test_undo_first_checkpoint(server: SyncServer, default_user):
    """
    If the block is at the first checkpoint (sequence_number=1),
    undo should fail because there's no prior checkpoint.
    """
    block_manager = BlockManager()

    # 1) Create the block
    block_data = PydanticBlock(label="first_checkpoint", value="Version1")
    block = block_manager.create_or_update_block(block_data, actor=default_user)

    # 2) First checkpoint => seq=1
    block_manager.checkpoint_block(block_id=block.id, actor=default_user)

    # Attempt undo -> expect ValueError
    with pytest.raises(ValueError, match="Cannot undo further"):
        block_manager.undo_checkpoint_block(block_id=block.id, actor=default_user)


def test_undo_multiple_checkpoints(server: SyncServer, default_user):
    """
    Tests multiple checkpoints in a row, then undo repeatedly
    from seq=3 -> seq=2 -> seq=1, verifying each revert.
    """
    block_manager = BlockManager()

    # Step 1: Create block
    block_data = PydanticBlock(label="multi_checkpoint", value="v1")
    block_v1 = block_manager.create_or_update_block(block_data, actor=default_user)
    # checkpoint => seq=1
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # Step 2: Update to v2, checkpoint => seq=2
    block_data_v2 = PydanticBlock(**block_v1.model_dump())
    block_data_v2.value = "v2"
    block_manager.create_or_update_block(block_data_v2, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # Step 3: Update to v3, checkpoint => seq=3
    block_data_v3 = PydanticBlock(**block_v1.model_dump())
    block_data_v3.value = "v3"
    block_manager.create_or_update_block(block_data_v3, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # Now we have 3 seq: v1, v2, v3
    # Undo from seq=3 -> seq=2
    undone_block = block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)
    assert undone_block.value == "v2"

    # Undo from seq=2 -> seq=1
    undone_block = block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)
    assert undone_block.value == "v1"

    # Try once more -> fails because seq=1 is the earliest
    with pytest.raises(ValueError, match="Cannot undo further"):
        block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)


def test_undo_concurrency_stale(server: SyncServer, default_user):
    """
    Demonstrate concurrency: both sessions start with the block at seq=2,
    one session undoes first -> block now seq=1, version increments,
    the other session tries to undo with stale data -> StaleDataError.
    """
    block_manager = BlockManager()

    # 1) create block
    block_data = PydanticBlock(label="concurrency_undo", value="v1")
    block_v1 = block_manager.create_or_update_block(block_data, actor=default_user)
    # checkpoint => seq=1
    block_manager.checkpoint_block(block_v1.id, actor=default_user)

    # 2) update to v2
    block_data_v2 = PydanticBlock(**block_v1.model_dump())
    block_data_v2.value = "v2"
    block_manager.create_or_update_block(block_data_v2, actor=default_user)
    # checkpoint => seq=2
    block_manager.checkpoint_block(block_v1.id, actor=default_user)

    # Now block is at seq=2

    # session1 preloads the block
    with db_context() as s1:
        block_s1 = s1.get(Block, block_v1.id)  # version=? let's say 2 in memory

    # session2 also preloads the block
    with db_context() as s2:
        block_s2 = s2.get(Block, block_v1.id)  # also version=2

    # Session1 -> undo to seq=1
    block_manager.undo_checkpoint_block(
        block_id=block_v1.id, actor=default_user, use_preloaded_block=block_s1  # stale object from session1
    )
    # This commits first => block now points to seq=1, version increments

    # Session2 tries the same undo, but it's stale
    with pytest.raises(StaleDataError):
        block_manager.undo_checkpoint_block(block_id=block_v1.id, actor=default_user, use_preloaded_block=block_s2)  # also seq=2 in memory


# ======================================================================================================================
# Block Manager Tests - Redo
# ======================================================================================================================


def test_redo_checkpoint_block(server: SyncServer, default_user):
    """
    1) Create a block with value v1 -> checkpoint => seq=1
    2) Update to v2 -> checkpoint => seq=2
    3) Update to v3 -> checkpoint => seq=3
    4) Undo once (seq=3 -> seq=2)
    5) Redo once (seq=2 -> seq=3)
    """

    block_manager = BlockManager()

    # 1) Create block, set value='v1'; checkpoint => seq=1
    block_v1 = block_manager.create_or_update_block(PydanticBlock(label="redo_test", value="v1"), actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # 2) Update to 'v2'; checkpoint => seq=2
    block_v2 = PydanticBlock(**block_v1.model_dump())
    block_v2.value = "v2"
    block_manager.create_or_update_block(block_v2, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # 3) Update to 'v3'; checkpoint => seq=3
    block_v3 = PydanticBlock(**block_v1.model_dump())
    block_v3.value = "v3"
    block_manager.create_or_update_block(block_v3, actor=default_user)
    block_manager.checkpoint_block(block_id=block_v1.id, actor=default_user)

    # Undo from seq=3 -> seq=2
    undone_block = block_manager.undo_checkpoint_block(block_v1.id, actor=default_user)
    assert undone_block.value == "v2", "After undo, block should revert to v2"

    # Redo from seq=2 -> seq=3
    redone_block = block_manager.redo_checkpoint_block(block_v1.id, actor=default_user)
    assert redone_block.value == "v3", "After redo, block should go back to v3"


def test_redo_no_history(server: SyncServer, default_user):
    """
    If a block has no current_history_entry_id (never checkpointed),
    then redo_checkpoint_block should raise ValueError.
    """
    block_manager = BlockManager()

    # Create block with no checkpoint
    block = block_manager.create_or_update_block(PydanticBlock(label="redo_no_history", value="v0"), actor=default_user)

    # Attempt to redo => expect ValueError
    with pytest.raises(ValueError, match="no history entry - cannot redo"):
        block_manager.redo_checkpoint_block(block.id, actor=default_user)


def test_redo_at_highest_checkpoint(server: SyncServer, default_user):
    """
    If the block is at the maximum sequence number, there's no higher checkpoint to move to.
    redo_checkpoint_block should raise ValueError.
    """
    block_manager = BlockManager()

    # 1) Create block => checkpoint => seq=1
    b_init = block_manager.create_or_update_block(PydanticBlock(label="redo_highest", value="v1"), actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # 2) Another edit => seq=2
    b_next = PydanticBlock(**b_init.model_dump())
    b_next.value = "v2"
    block_manager.create_or_update_block(b_next, actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # We are at seq=2, which is the highest checkpoint.
    # Attempt redo => there's no seq=3
    with pytest.raises(ValueError, match="Cannot redo further"):
        block_manager.redo_checkpoint_block(b_init.id, actor=default_user)


def test_redo_after_multiple_undo(server: SyncServer, default_user):
    """
    1) Create and checkpoint versions: v1 -> seq=1, v2 -> seq=2, v3 -> seq=3, v4 -> seq=4
    2) Undo thrice => from seq=4 to seq=1
    3) Redo thrice => from seq=1 back to seq=4
    """
    block_manager = BlockManager()

    # Step 1: create initial block => seq=1
    b_init = block_manager.create_or_update_block(PydanticBlock(label="redo_multi", value="v1"), actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # seq=2
    b_v2 = PydanticBlock(**b_init.model_dump())
    b_v2.value = "v2"
    block_manager.create_or_update_block(b_v2, actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # seq=3
    b_v3 = PydanticBlock(**b_init.model_dump())
    b_v3.value = "v3"
    block_manager.create_or_update_block(b_v3, actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # seq=4
    b_v4 = PydanticBlock(**b_init.model_dump())
    b_v4.value = "v4"
    block_manager.create_or_update_block(b_v4, actor=default_user)
    block_manager.checkpoint_block(b_init.id, actor=default_user)

    # We have 4 checkpoints: v1...v4. Current is seq=4.

    # 2) Undo thrice => from seq=4 -> seq=1
    for expected_value in ["v3", "v2", "v1"]:
        undone_block = block_manager.undo_checkpoint_block(b_init.id, actor=default_user)
        assert undone_block.value == expected_value, f"Undo should get us back to {expected_value}"

    # 3) Redo thrice => from seq=1 -> seq=4
    for expected_value in ["v2", "v3", "v4"]:
        redone_block = block_manager.redo_checkpoint_block(b_init.id, actor=default_user)
        assert redone_block.value == expected_value, f"Redo should get us forward to {expected_value}"


def test_redo_concurrency_stale(server: SyncServer, default_user):
    block_manager = BlockManager()

    # 1) Create block => checkpoint => seq=1
    block = block_manager.create_or_update_block(PydanticBlock(label="redo_concurrency", value="v1"), actor=default_user)
    block_manager.checkpoint_block(block.id, actor=default_user)

    # 2) Another edit => checkpoint => seq=2
    block_v2 = PydanticBlock(**block.model_dump())
    block_v2.value = "v2"
    block_manager.create_or_update_block(block_v2, actor=default_user)
    block_manager.checkpoint_block(block.id, actor=default_user)

    # 3) Another edit => checkpoint => seq=3
    block_v3 = PydanticBlock(**block.model_dump())
    block_v3.value = "v3"
    block_manager.create_or_update_block(block_v3, actor=default_user)
    block_manager.checkpoint_block(block.id, actor=default_user)
    # Now the block is at seq=3 in the DB

    # 4) Undo from seq=3 -> seq=2 so that we have a known future state at seq=3
    undone_block = block_manager.undo_checkpoint_block(block.id, actor=default_user)
    assert undone_block.value == "v2"

    # At this point the block is physically at seq=2 in DB,
    # but there's a valid row for seq=3 in block_history (the 'v3' state).

    # 5) Simulate concurrency: two sessions each read the block at seq=2
    with db_context() as s1:
        block_s1 = s1.get(Block, block.id)
    with db_context() as s2:
        block_s2 = s2.get(Block, block.id)

    # 6) Session1 redoes to seq=3 first -> success
    block_manager.redo_checkpoint_block(block_id=block.id, actor=default_user, use_preloaded_block=block_s1)
    # commits => block is now seq=3 in DB, version increments

    # 7) Session2 tries to do the same from stale version
    # => we expect StaleDataError, because the second session is using
    #    an out-of-date version of the block
    with pytest.raises(StaleDataError):
        block_manager.redo_checkpoint_block(block_id=block.id, actor=default_user, use_preloaded_block=block_s2)


# ======================================================================================================================
# Identity Manager Tests
# ======================================================================================================================


def test_create_and_upsert_identity(server: SyncServer, default_user):
    identity_create = IdentityCreate(
        identifier_key="1234",
        name="caren",
        identity_type=IdentityType.user,
        properties=[
            IdentityProperty(key="email", value="caren@letta.com", type=IdentityPropertyType.string),
            IdentityProperty(key="age", value=28, type=IdentityPropertyType.number),
        ],
    )

    identity = server.identity_manager.create_identity(identity_create, actor=default_user)

    # Assertions to ensure the created identity matches the expected values
    assert identity.identifier_key == identity_create.identifier_key
    assert identity.name == identity_create.name
    assert identity.identity_type == identity_create.identity_type
    assert identity.properties == identity_create.properties
    assert identity.agent_ids == []
    assert identity.project_id == None

    with pytest.raises(UniqueConstraintViolationError):
        server.identity_manager.create_identity(
            IdentityCreate(identifier_key="1234", name="sarah", identity_type=IdentityType.user),
            actor=default_user,
        )

    identity_create.properties = [(IdentityProperty(key="age", value=29, type=IdentityPropertyType.number))]

    identity = server.identity_manager.upsert_identity(identity=IdentityUpsert(**identity_create.model_dump()), actor=default_user)

    identity = server.identity_manager.get_identity(identity_id=identity.id, actor=default_user)
    assert len(identity.properties) == 1
    assert identity.properties[0].key == "age"
    assert identity.properties[0].value == 29

    server.identity_manager.delete_identity(identity_id=identity.id, actor=default_user)


def test_get_identities(server, default_user):
    # Create identities to retrieve later
    user = server.identity_manager.create_identity(
        IdentityCreate(name="caren", identifier_key="1234", identity_type=IdentityType.user), actor=default_user
    )
    org = server.identity_manager.create_identity(
        IdentityCreate(name="letta", identifier_key="0001", identity_type=IdentityType.org), actor=default_user
    )

    # Retrieve identities by different filters
    all_identities = server.identity_manager.list_identities(actor=default_user)
    assert len(all_identities) == 2

    user_identities = server.identity_manager.list_identities(actor=default_user, identity_type=IdentityType.user)
    assert len(user_identities) == 1
    assert user_identities[0].name == user.name

    org_identities = server.identity_manager.list_identities(actor=default_user, identity_type=IdentityType.org)
    assert len(org_identities) == 1
    assert org_identities[0].name == org.name

    server.identity_manager.delete_identity(identity_id=user.id, actor=default_user)
    server.identity_manager.delete_identity(identity_id=org.id, actor=default_user)


def test_update_identity(server: SyncServer, sarah_agent, charles_agent, default_user):
    identity = server.identity_manager.create_identity(
        IdentityCreate(name="caren", identifier_key="1234", identity_type=IdentityType.user), actor=default_user
    )

    # Update identity fields
    update_data = IdentityUpdate(
        agent_ids=[sarah_agent.id, charles_agent.id],
        properties=[IdentityProperty(key="email", value="caren@letta.com", type=IdentityPropertyType.string)],
    )
    server.identity_manager.update_identity(identity_id=identity.id, identity=update_data, actor=default_user)

    # Retrieve the updated identity
    updated_identity = server.identity_manager.get_identity(identity_id=identity.id, actor=default_user)

    # Assertions to verify the update
    assert updated_identity.agent_ids.sort() == update_data.agent_ids.sort()
    assert updated_identity.properties == update_data.properties

    agent_state = server.agent_manager.get_agent_by_id(agent_id=sarah_agent.id, actor=default_user)
    assert identity.id in agent_state.identity_ids
    agent_state = server.agent_manager.get_agent_by_id(agent_id=charles_agent.id, actor=default_user)
    assert identity.id in agent_state.identity_ids

    server.identity_manager.delete_identity(identity_id=identity.id, actor=default_user)


def test_attach_detach_identity_from_agent(server: SyncServer, sarah_agent, default_user):
    # Create an identity
    identity = server.identity_manager.create_identity(
        IdentityCreate(name="caren", identifier_key="1234", identity_type=IdentityType.user), actor=default_user
    )
    agent_state = server.agent_manager.update_agent(
        agent_id=sarah_agent.id, agent_update=UpdateAgent(identity_ids=[identity.id]), actor=default_user
    )

    # Check that identity has been attached
    assert identity.id in agent_state.identity_ids

    # Now attempt to delete the identity
    server.identity_manager.delete_identity(identity_id=identity.id, actor=default_user)

    # Verify that the identity was deleted
    identities = server.identity_manager.list_identities(actor=default_user)
    assert len(identities) == 0

    # Check that block has been detached too
    agent_state = server.agent_manager.get_agent_by_id(agent_id=sarah_agent.id, actor=default_user)
    assert not identity.id in agent_state.identity_ids


def test_get_set_agents_for_identities(server: SyncServer, sarah_agent, charles_agent, default_user):
    identity = server.identity_manager.create_identity(
        IdentityCreate(name="caren", identifier_key="1234", identity_type=IdentityType.user, agent_ids=[sarah_agent.id, charles_agent.id]),
        actor=default_user,
    )

    agent_with_identity = server.create_agent(
        CreateAgent(
            memory_blocks=[],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            identity_ids=[identity.id],
            include_base_tools=False,
        ),
        actor=default_user,
    )
    agent_without_identity = server.create_agent(
        CreateAgent(
            memory_blocks=[],
            llm_config=LLMConfig.default_config("gpt-4"),
            embedding_config=EmbeddingConfig.default_config(provider="openai"),
            include_base_tools=False,
        ),
        actor=default_user,
    )

    # Get the agents for identity id
    agent_states = server.agent_manager.list_agents(identity_id=identity.id, actor=default_user)
    assert len(agent_states) == 3

    # Check all agents are in the list
    agent_state_ids = [a.id for a in agent_states]
    assert sarah_agent.id in agent_state_ids
    assert charles_agent.id in agent_state_ids
    assert agent_with_identity.id in agent_state_ids
    assert not agent_without_identity.id in agent_state_ids

    # Get the agents for identifier key
    agent_states = server.agent_manager.list_agents(identifier_keys=[identity.identifier_key], actor=default_user)
    assert len(agent_states) == 3

    # Check all agents are in the list
    agent_state_ids = [a.id for a in agent_states]
    assert sarah_agent.id in agent_state_ids
    assert charles_agent.id in agent_state_ids
    assert agent_with_identity.id in agent_state_ids
    assert not agent_without_identity.id in agent_state_ids

    # Delete new agents
    server.agent_manager.delete_agent(agent_id=agent_with_identity.id, actor=default_user)
    server.agent_manager.delete_agent(agent_id=agent_without_identity.id, actor=default_user)

    # Get the agents for identity id
    agent_states = server.agent_manager.list_agents(identity_id=identity.id, actor=default_user)
    assert len(agent_states) == 2

    # Check only initial agents are in the list
    agent_state_ids = [a.id for a in agent_states]
    assert sarah_agent.id in agent_state_ids
    assert charles_agent.id in agent_state_ids

    server.identity_manager.delete_identity(identity_id=identity.id, actor=default_user)


def test_attach_detach_identity_from_block(server: SyncServer, default_block, default_user):
    # Create an identity
    identity = server.identity_manager.create_identity(
        IdentityCreate(name="caren", identifier_key="1234", identity_type=IdentityType.user, block_ids=[default_block.id]),
        actor=default_user,
    )

    # Check that identity has been attached
    blocks = server.block_manager.get_blocks(identity_id=identity.id, actor=default_user)
    assert len(blocks) == 1 and blocks[0].id == default_block.id

    # Now attempt to delete the identity
    server.identity_manager.delete_identity(identity_id=identity.id, actor=default_user)

    # Verify that the identity was deleted
    identities = server.identity_manager.list_identities(actor=default_user)
    assert len(identities) == 0

    # Check that block has been detached too
    blocks = server.block_manager.get_blocks(identity_id=identity.id, actor=default_user)
    assert len(blocks) == 0


def test_get_set_blocks_for_identities(server: SyncServer, default_block, default_user):
    block_manager = BlockManager()
    block_with_identity = block_manager.create_or_update_block(PydanticBlock(label="persona", value="Original Content"), actor=default_user)
    block_without_identity = block_manager.create_or_update_block(PydanticBlock(label="user", value="Original Content"), actor=default_user)
    identity = server.identity_manager.create_identity(
        IdentityCreate(
            name="caren", identifier_key="1234", identity_type=IdentityType.user, block_ids=[default_block.id, block_with_identity.id]
        ),
        actor=default_user,
    )

    # Get the blocks for identity id
    blocks = server.block_manager.get_blocks(identity_id=identity.id, actor=default_user)
    assert len(blocks) == 2

    # Check blocks are in the list
    block_ids = [b.id for b in blocks]
    assert default_block.id in block_ids
    assert block_with_identity.id in block_ids
    assert not block_without_identity.id in block_ids

    # Get the blocks for identifier key
    blocks = server.block_manager.get_blocks(identifier_keys=[identity.identifier_key], actor=default_user)
    assert len(blocks) == 2

    # Check blocks are in the list
    block_ids = [b.id for b in blocks]
    assert default_block.id in block_ids
    assert block_with_identity.id in block_ids
    assert not block_without_identity.id in block_ids

    # Delete new agents
    server.block_manager.delete_block(block_id=block_with_identity.id, actor=default_user)
    server.block_manager.delete_block(block_id=block_without_identity.id, actor=default_user)

    # Get the blocks for identity id
    blocks = server.block_manager.get_blocks(identity_id=identity.id, actor=default_user)
    assert len(blocks) == 1

    # Check only initial block in the list
    block_ids = [b.id for b in blocks]
    assert default_block.id in block_ids
    assert not block_with_identity.id in block_ids
    assert not block_without_identity.id in block_ids

    server.identity_manager.delete_identity(identity.id, actor=default_user)


def test_upsert_properties(server: SyncServer, default_user):
    identity_create = IdentityCreate(
        identifier_key="1234",
        name="caren",
        identity_type=IdentityType.user,
        properties=[
            IdentityProperty(key="email", value="caren@letta.com", type=IdentityPropertyType.string),
            IdentityProperty(key="age", value=28, type=IdentityPropertyType.number),
        ],
    )

    identity = server.identity_manager.create_identity(identity_create, actor=default_user)
    properties = [
        IdentityProperty(key="email", value="caren@gmail.com", type=IdentityPropertyType.string),
        IdentityProperty(key="age", value="28", type=IdentityPropertyType.string),
        IdentityProperty(key="test", value=123, type=IdentityPropertyType.number),
    ]

    updated_identity = server.identity_manager.upsert_identity_properties(
        identity_id=identity.id,
        properties=properties,
        actor=default_user,
    )
    assert updated_identity.properties == properties

    server.identity_manager.delete_identity(identity.id, actor=default_user)


# ======================================================================================================================
# SourceManager Tests - Sources
# ======================================================================================================================
def test_create_source(server: SyncServer, default_user):
    """Test creating a new source."""
    source_pydantic = PydanticSource(
        name="Test Source",
        description="This is a test source.",
        metadata={"type": "test"},
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Assertions to check the created source
    assert source.name == source_pydantic.name
    assert source.description == source_pydantic.description
    assert source.metadata == source_pydantic.metadata
    assert source.organization_id == default_user.organization_id


def test_create_sources_with_same_name_does_not_error(server: SyncServer, default_user):
    """Test creating a new source."""
    name = "Test Source"
    source_pydantic = PydanticSource(
        name=name,
        description="This is a test source.",
        metadata={"type": "medical"},
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)
    source_pydantic = PydanticSource(
        name=name,
        description="This is a different test source.",
        metadata={"type": "legal"},
        embedding_config=DEFAULT_EMBEDDING_CONFIG,
    )
    same_source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    assert source.name == same_source.name
    assert source.id != same_source.id


def test_update_source(server: SyncServer, default_user):
    """Test updating an existing source."""
    source_pydantic = PydanticSource(name="Original Source", description="Original description", embedding_config=DEFAULT_EMBEDDING_CONFIG)
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Update the source
    update_data = SourceUpdate(name="Updated Source", description="Updated description", metadata={"type": "updated"})
    updated_source = server.source_manager.update_source(source_id=source.id, source_update=update_data, actor=default_user)

    # Assertions to verify update
    assert updated_source.name == update_data.name
    assert updated_source.description == update_data.description
    assert updated_source.metadata == update_data.metadata


def test_delete_source(server: SyncServer, default_user):
    """Test deleting a source."""
    source_pydantic = PydanticSource(
        name="To Delete", description="This source will be deleted.", embedding_config=DEFAULT_EMBEDDING_CONFIG
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Delete the source
    deleted_source = server.source_manager.delete_source(source_id=source.id, actor=default_user)

    # Assertions to verify deletion
    assert deleted_source.id == source.id

    # Verify that the source no longer appears in list_sources
    sources = server.source_manager.list_sources(actor=default_user)
    assert len(sources) == 0


def test_delete_attached_source(server: SyncServer, sarah_agent, default_user):
    """Test deleting a source."""
    source_pydantic = PydanticSource(
        name="To Delete", description="This source will be deleted.", embedding_config=DEFAULT_EMBEDDING_CONFIG
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    server.agent_manager.attach_source(agent_id=sarah_agent.id, source_id=source.id, actor=default_user)

    # Delete the source
    deleted_source = server.source_manager.delete_source(source_id=source.id, actor=default_user)

    # Assertions to verify deletion
    assert deleted_source.id == source.id

    # Verify that the source no longer appears in list_sources
    sources = server.source_manager.list_sources(actor=default_user)
    assert len(sources) == 0

    # Verify that agent is not deleted
    agent = server.agent_manager.get_agent_by_id(sarah_agent.id, actor=default_user)
    assert agent is not None


def test_list_sources(server: SyncServer, default_user):
    """Test listing sources with pagination."""
    # Create multiple sources
    server.source_manager.create_source(PydanticSource(name="Source 1", embedding_config=DEFAULT_EMBEDDING_CONFIG), actor=default_user)
    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)
    server.source_manager.create_source(PydanticSource(name="Source 2", embedding_config=DEFAULT_EMBEDDING_CONFIG), actor=default_user)

    # List sources without pagination
    sources = server.source_manager.list_sources(actor=default_user)
    assert len(sources) == 2

    # List sources with pagination
    paginated_sources = server.source_manager.list_sources(actor=default_user, limit=1)
    assert len(paginated_sources) == 1

    # Ensure cursor-based pagination works
    next_page = server.source_manager.list_sources(actor=default_user, after=paginated_sources[-1].id, limit=1)
    assert len(next_page) == 1
    assert next_page[0].name != paginated_sources[0].name


def test_get_source_by_id(server: SyncServer, default_user):
    """Test retrieving a source by ID."""
    source_pydantic = PydanticSource(
        name="Retrieve by ID", description="Test source for ID retrieval", embedding_config=DEFAULT_EMBEDDING_CONFIG
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Retrieve the source by ID
    retrieved_source = server.source_manager.get_source_by_id(source_id=source.id, actor=default_user)

    # Assertions to verify the retrieved source matches the created one
    assert retrieved_source.id == source.id
    assert retrieved_source.name == source.name
    assert retrieved_source.description == source.description


def test_get_source_by_name(server: SyncServer, default_user):
    """Test retrieving a source by name."""
    source_pydantic = PydanticSource(
        name="Unique Source", description="Test source for name retrieval", embedding_config=DEFAULT_EMBEDDING_CONFIG
    )
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Retrieve the source by name
    retrieved_source = server.source_manager.get_source_by_name(source_name=source.name, actor=default_user)

    # Assertions to verify the retrieved source matches the created one
    assert retrieved_source.name == source.name
    assert retrieved_source.description == source.description


def test_update_source_no_changes(server: SyncServer, default_user):
    """Test update_source with no actual changes to verify logging and response."""
    source_pydantic = PydanticSource(name="No Change Source", description="No changes", embedding_config=DEFAULT_EMBEDDING_CONFIG)
    source = server.source_manager.create_source(source=source_pydantic, actor=default_user)

    # Attempt to update the source with identical data
    update_data = SourceUpdate(name="No Change Source", description="No changes")
    updated_source = server.source_manager.update_source(source_id=source.id, source_update=update_data, actor=default_user)

    # Assertions to ensure the update returned the source but made no modifications
    assert updated_source.id == source.id
    assert updated_source.name == source.name
    assert updated_source.description == source.description


# ======================================================================================================================
# Source Manager Tests - Files
# ======================================================================================================================


def test_get_file_by_id(server: SyncServer, default_user, default_source):
    """Test retrieving a file by ID."""
    file_metadata = PydanticFileMetadata(
        file_name="Retrieve File",
        file_path="/path/to/retrieve_file.txt",
        file_type="text/plain",
        file_size=2048,
        source_id=default_source.id,
    )
    created_file = server.source_manager.create_file(file_metadata=file_metadata, actor=default_user)

    # Retrieve the file by ID
    retrieved_file = server.source_manager.get_file_by_id(file_id=created_file.id, actor=default_user)

    # Assertions to verify the retrieved file matches the created one
    assert retrieved_file.id == created_file.id
    assert retrieved_file.file_name == created_file.file_name
    assert retrieved_file.file_path == created_file.file_path
    assert retrieved_file.file_type == created_file.file_type


def test_list_files(server: SyncServer, default_user, default_source):
    """Test listing files with pagination."""
    # Create multiple files
    server.source_manager.create_file(
        PydanticFileMetadata(file_name="File 1", file_path="/path/to/file1.txt", file_type="text/plain", source_id=default_source.id),
        actor=default_user,
    )
    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)
    server.source_manager.create_file(
        PydanticFileMetadata(file_name="File 2", file_path="/path/to/file2.txt", file_type="text/plain", source_id=default_source.id),
        actor=default_user,
    )

    # List files without pagination
    files = server.source_manager.list_files(source_id=default_source.id, actor=default_user)
    assert len(files) == 2

    # List files with pagination
    paginated_files = server.source_manager.list_files(source_id=default_source.id, actor=default_user, limit=1)
    assert len(paginated_files) == 1

    # Ensure cursor-based pagination works
    next_page = server.source_manager.list_files(source_id=default_source.id, actor=default_user, after=paginated_files[-1].id, limit=1)
    assert len(next_page) == 1
    assert next_page[0].file_name != paginated_files[0].file_name


def test_delete_file(server: SyncServer, default_user, default_source):
    """Test deleting a file."""
    file_metadata = PydanticFileMetadata(
        file_name="Delete File", file_path="/path/to/delete_file.txt", file_type="text/plain", source_id=default_source.id
    )
    created_file = server.source_manager.create_file(file_metadata=file_metadata, actor=default_user)

    # Delete the file
    deleted_file = server.source_manager.delete_file(file_id=created_file.id, actor=default_user)

    # Assertions to verify deletion
    assert deleted_file.id == created_file.id

    # Verify that the file no longer appears in list_files
    files = server.source_manager.list_files(source_id=default_source.id, actor=default_user)
    assert len(files) == 0


# ======================================================================================================================
# SandboxConfigManager Tests - Sandbox Configs
# ======================================================================================================================


def test_create_or_update_sandbox_config(server: SyncServer, default_user):
    sandbox_config_create = SandboxConfigCreate(
        config=E2BSandboxConfig(),
    )
    created_config = server.sandbox_config_manager.create_or_update_sandbox_config(sandbox_config_create, actor=default_user)

    # Assertions
    assert created_config.type == SandboxType.E2B
    assert created_config.get_e2b_config() == sandbox_config_create.config
    assert created_config.organization_id == default_user.organization_id


def test_create_local_sandbox_config_defaults(server: SyncServer, default_user):
    sandbox_config_create = SandboxConfigCreate(
        config=LocalSandboxConfig(),
    )
    created_config = server.sandbox_config_manager.create_or_update_sandbox_config(sandbox_config_create, actor=default_user)

    # Assertions
    assert created_config.type == SandboxType.LOCAL
    assert created_config.get_local_config() == sandbox_config_create.config
    assert created_config.get_local_config().sandbox_dir in {LETTA_TOOL_EXECUTION_DIR, tool_settings.local_sandbox_dir}
    assert created_config.organization_id == default_user.organization_id


def test_default_e2b_settings_sandbox_config(server: SyncServer, default_user):
    created_config = server.sandbox_config_manager.get_or_create_default_sandbox_config(sandbox_type=SandboxType.E2B, actor=default_user)
    e2b_config = created_config.get_e2b_config()

    # Assertions
    assert e2b_config.timeout == 5 * 60
    assert e2b_config.template == tool_settings.e2b_sandbox_template_id


def test_update_existing_sandbox_config(server: SyncServer, sandbox_config_fixture, default_user):
    update_data = SandboxConfigUpdate(config=E2BSandboxConfig(template="template_2", timeout=120))
    updated_config = server.sandbox_config_manager.update_sandbox_config(sandbox_config_fixture.id, update_data, actor=default_user)

    # Assertions
    assert updated_config.config["template"] == "template_2"
    assert updated_config.config["timeout"] == 120


def test_delete_sandbox_config(server: SyncServer, sandbox_config_fixture, default_user):
    deleted_config = server.sandbox_config_manager.delete_sandbox_config(sandbox_config_fixture.id, actor=default_user)

    # Assertions to verify deletion
    assert deleted_config.id == sandbox_config_fixture.id

    # Verify it no longer exists
    config_list = server.sandbox_config_manager.list_sandbox_configs(actor=default_user)
    assert sandbox_config_fixture.id not in [config.id for config in config_list]


def test_get_sandbox_config_by_type(server: SyncServer, sandbox_config_fixture, default_user):
    retrieved_config = server.sandbox_config_manager.get_sandbox_config_by_type(sandbox_config_fixture.type, actor=default_user)

    # Assertions to verify correct retrieval
    assert retrieved_config.id == sandbox_config_fixture.id
    assert retrieved_config.type == sandbox_config_fixture.type


def test_list_sandbox_configs(server: SyncServer, default_user):
    # Creating multiple sandbox configs
    config_e2b_create = SandboxConfigCreate(
        config=E2BSandboxConfig(),
    )
    config_local_create = SandboxConfigCreate(
        config=LocalSandboxConfig(sandbox_dir=""),
    )
    config_e2b = server.sandbox_config_manager.create_or_update_sandbox_config(config_e2b_create, actor=default_user)
    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)
    config_local = server.sandbox_config_manager.create_or_update_sandbox_config(config_local_create, actor=default_user)

    # List configs without pagination
    configs = server.sandbox_config_manager.list_sandbox_configs(actor=default_user)
    assert len(configs) >= 2

    # List configs with pagination
    paginated_configs = server.sandbox_config_manager.list_sandbox_configs(actor=default_user, limit=1)
    assert len(paginated_configs) == 1

    next_page = server.sandbox_config_manager.list_sandbox_configs(actor=default_user, after=paginated_configs[-1].id, limit=1)
    assert len(next_page) == 1
    assert next_page[0].id != paginated_configs[0].id

    # List configs using sandbox_type filter
    configs = server.sandbox_config_manager.list_sandbox_configs(actor=default_user, sandbox_type=SandboxType.E2B)
    assert len(configs) == 1
    assert configs[0].id == config_e2b.id

    configs = server.sandbox_config_manager.list_sandbox_configs(actor=default_user, sandbox_type=SandboxType.LOCAL)
    assert len(configs) == 1
    assert configs[0].id == config_local.id


# ======================================================================================================================
# SandboxConfigManager Tests - Environment Variables
# ======================================================================================================================


def test_create_sandbox_env_var(server: SyncServer, sandbox_config_fixture, default_user):
    env_var_create = SandboxEnvironmentVariableCreate(key="TEST_VAR", value="test_value", description="A test environment variable.")
    created_env_var = server.sandbox_config_manager.create_sandbox_env_var(
        env_var_create, sandbox_config_id=sandbox_config_fixture.id, actor=default_user
    )

    # Assertions
    assert created_env_var.key == env_var_create.key
    assert created_env_var.value == env_var_create.value
    assert created_env_var.organization_id == default_user.organization_id


def test_update_sandbox_env_var(server: SyncServer, sandbox_env_var_fixture, default_user):
    update_data = SandboxEnvironmentVariableUpdate(value="updated_value")
    updated_env_var = server.sandbox_config_manager.update_sandbox_env_var(sandbox_env_var_fixture.id, update_data, actor=default_user)

    # Assertions
    assert updated_env_var.value == "updated_value"
    assert updated_env_var.id == sandbox_env_var_fixture.id


def test_delete_sandbox_env_var(server: SyncServer, sandbox_config_fixture, sandbox_env_var_fixture, default_user):
    deleted_env_var = server.sandbox_config_manager.delete_sandbox_env_var(sandbox_env_var_fixture.id, actor=default_user)

    # Assertions to verify deletion
    assert deleted_env_var.id == sandbox_env_var_fixture.id

    # Verify it no longer exists
    env_vars = server.sandbox_config_manager.list_sandbox_env_vars(sandbox_config_id=sandbox_config_fixture.id, actor=default_user)
    assert sandbox_env_var_fixture.id not in [env_var.id for env_var in env_vars]


def test_list_sandbox_env_vars(server: SyncServer, sandbox_config_fixture, default_user):
    # Creating multiple environment variables
    env_var_create_a = SandboxEnvironmentVariableCreate(key="VAR1", value="value1")
    env_var_create_b = SandboxEnvironmentVariableCreate(key="VAR2", value="value2")
    server.sandbox_config_manager.create_sandbox_env_var(env_var_create_a, sandbox_config_id=sandbox_config_fixture.id, actor=default_user)
    if USING_SQLITE:
        time.sleep(CREATE_DELAY_SQLITE)
    server.sandbox_config_manager.create_sandbox_env_var(env_var_create_b, sandbox_config_id=sandbox_config_fixture.id, actor=default_user)

    # List env vars without pagination
    env_vars = server.sandbox_config_manager.list_sandbox_env_vars(sandbox_config_id=sandbox_config_fixture.id, actor=default_user)
    assert len(env_vars) >= 2

    # List env vars with pagination
    paginated_env_vars = server.sandbox_config_manager.list_sandbox_env_vars(
        sandbox_config_id=sandbox_config_fixture.id, actor=default_user, limit=1
    )
    assert len(paginated_env_vars) == 1

    next_page = server.sandbox_config_manager.list_sandbox_env_vars(
        sandbox_config_id=sandbox_config_fixture.id, actor=default_user, after=paginated_env_vars[-1].id, limit=1
    )
    assert len(next_page) == 1
    assert next_page[0].id != paginated_env_vars[0].id


def test_get_sandbox_env_var_by_key(server: SyncServer, sandbox_env_var_fixture, default_user):
    retrieved_env_var = server.sandbox_config_manager.get_sandbox_env_var_by_key_and_sandbox_config_id(
        sandbox_env_var_fixture.key, sandbox_env_var_fixture.sandbox_config_id, actor=default_user
    )

    # Assertions to verify correct retrieval
    assert retrieved_env_var.id == sandbox_env_var_fixture.id
    assert retrieved_env_var.key == sandbox_env_var_fixture.key


# ======================================================================================================================
# JobManager Tests
# ======================================================================================================================


def test_create_job(server: SyncServer, default_user):
    """Test creating a job."""
    job_data = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test"},
    )

    created_job = server.job_manager.create_job(job_data, actor=default_user)

    # Assertions to ensure the created job matches the expected values
    assert created_job.user_id == default_user.id
    assert created_job.status == JobStatus.created
    assert created_job.metadata == {"type": "test"}


def test_get_job_by_id(server: SyncServer, default_user):
    """Test fetching a job by ID."""
    # Create a job
    job_data = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test"},
    )
    created_job = server.job_manager.create_job(job_data, actor=default_user)

    # Fetch the job by ID
    fetched_job = server.job_manager.get_job_by_id(created_job.id, actor=default_user)

    # Assertions to ensure the fetched job matches the created job
    assert fetched_job.id == created_job.id
    assert fetched_job.status == JobStatus.created
    assert fetched_job.metadata == {"type": "test"}


def test_list_jobs(server: SyncServer, default_user):
    """Test listing jobs."""
    # Create multiple jobs
    for i in range(3):
        job_data = PydanticJob(
            status=JobStatus.created,
            metadata={"type": f"test-{i}"},
        )
        server.job_manager.create_job(job_data, actor=default_user)

    # List jobs
    jobs = server.job_manager.list_jobs(actor=default_user)

    # Assertions to check that the created jobs are listed
    assert len(jobs) == 3
    assert all(job.user_id == default_user.id for job in jobs)
    assert all(job.metadata["type"].startswith("test") for job in jobs)


def test_update_job_by_id(server: SyncServer, default_user):
    """Test updating a job by its ID."""
    # Create a job
    job_data = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test"},
    )
    created_job = server.job_manager.create_job(job_data, actor=default_user)
    assert created_job.metadata == {"type": "test"}

    # Update the job
    update_data = JobUpdate(status=JobStatus.completed, metadata={"type": "updated"})
    updated_job = server.job_manager.update_job_by_id(created_job.id, update_data, actor=default_user)

    # Assertions to ensure the job was updated
    assert updated_job.status == JobStatus.completed
    assert updated_job.metadata == {"type": "updated"}
    assert updated_job.completed_at is not None


def test_delete_job_by_id(server: SyncServer, default_user):
    """Test deleting a job by its ID."""
    # Create a job
    job_data = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test"},
    )
    created_job = server.job_manager.create_job(job_data, actor=default_user)

    # Delete the job
    server.job_manager.delete_job_by_id(created_job.id, actor=default_user)

    # List jobs to ensure the job was deleted
    jobs = server.job_manager.list_jobs(actor=default_user)
    assert len(jobs) == 0


def test_update_job_auto_complete(server: SyncServer, default_user):
    """Test that updating a job's status to 'completed' automatically sets completed_at."""
    # Create a job
    job_data = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test"},
    )
    created_job = server.job_manager.create_job(job_data, actor=default_user)

    # Update the job's status to 'completed'
    update_data = JobUpdate(status=JobStatus.completed)
    updated_job = server.job_manager.update_job_by_id(created_job.id, update_data, actor=default_user)

    # Assertions to check that completed_at was set
    assert updated_job.status == JobStatus.completed
    assert updated_job.completed_at is not None


def test_get_job_not_found(server: SyncServer, default_user):
    """Test fetching a non-existent job."""
    non_existent_job_id = "nonexistent-id"
    with pytest.raises(NoResultFound):
        server.job_manager.get_job_by_id(non_existent_job_id, actor=default_user)


def test_delete_job_not_found(server: SyncServer, default_user):
    """Test deleting a non-existent job."""
    non_existent_job_id = "nonexistent-id"
    with pytest.raises(NoResultFound):
        server.job_manager.delete_job_by_id(non_existent_job_id, actor=default_user)


def test_list_jobs_pagination(server: SyncServer, default_user):
    """Test listing jobs with pagination."""
    # Create multiple jobs
    for i in range(10):
        job_data = PydanticJob(
            status=JobStatus.created,
            metadata={"type": f"test-{i}"},
        )
        server.job_manager.create_job(job_data, actor=default_user)

    # List jobs with a limit
    jobs = server.job_manager.list_jobs(actor=default_user, limit=5)
    assert len(jobs) == 5
    assert all(job.user_id == default_user.id for job in jobs)

    # Test cursor-based pagination
    first_page = server.job_manager.list_jobs(actor=default_user, limit=3, ascending=True)  # [J0, J1, J2]
    assert len(first_page) == 3
    assert first_page[0].created_at <= first_page[1].created_at <= first_page[2].created_at

    last_page = server.job_manager.list_jobs(actor=default_user, limit=3, ascending=False)  # [J9, J8, J7]
    assert len(last_page) == 3
    assert last_page[0].created_at >= last_page[1].created_at >= last_page[2].created_at
    first_page_ids = set(job.id for job in first_page)
    last_page_ids = set(job.id for job in last_page)
    assert first_page_ids.isdisjoint(last_page_ids)

    # Test middle page using both before and after
    middle_page = server.job_manager.list_jobs(
        actor=default_user, before=last_page[-1].id, after=first_page[-1].id, ascending=True
    )  # [J3, J4, J5, J6]
    assert len(middle_page) == 4  # Should include jobs between first and second page
    head_tail_jobs = first_page_ids.union(last_page_ids)
    assert all(job.id not in head_tail_jobs for job in middle_page)

    # Test descending order
    middle_page_desc = server.job_manager.list_jobs(
        actor=default_user, before=last_page[-1].id, after=first_page[-1].id, ascending=False
    )  # [J6, J5, J4, J3]
    assert len(middle_page_desc) == 4
    assert middle_page_desc[0].id == middle_page[-1].id
    assert middle_page_desc[1].id == middle_page[-2].id
    assert middle_page_desc[2].id == middle_page[-3].id
    assert middle_page_desc[3].id == middle_page[-4].id

    # BONUS
    job_7 = last_page[-1].id
    earliest_jobs = server.job_manager.list_jobs(actor=default_user, ascending=False, before=job_7)
    assert len(earliest_jobs) == 7
    assert all(j.id not in last_page_ids for j in earliest_jobs)
    assert all(earliest_jobs[i].created_at >= earliest_jobs[i + 1].created_at for i in range(len(earliest_jobs) - 1))


def test_list_jobs_by_status(server: SyncServer, default_user):
    """Test listing jobs filtered by status."""
    # Create multiple jobs with different statuses
    job_data_created = PydanticJob(
        status=JobStatus.created,
        metadata={"type": "test-created"},
    )
    job_data_in_progress = PydanticJob(
        status=JobStatus.running,
        metadata={"type": "test-running"},
    )
    job_data_completed = PydanticJob(
        status=JobStatus.completed,
        metadata={"type": "test-completed"},
    )

    server.job_manager.create_job(job_data_created, actor=default_user)
    server.job_manager.create_job(job_data_in_progress, actor=default_user)
    server.job_manager.create_job(job_data_completed, actor=default_user)

    # List jobs filtered by status
    created_jobs = server.job_manager.list_jobs(actor=default_user, statuses=[JobStatus.created])
    in_progress_jobs = server.job_manager.list_jobs(actor=default_user, statuses=[JobStatus.running])
    completed_jobs = server.job_manager.list_jobs(actor=default_user, statuses=[JobStatus.completed])

    # Assertions
    assert len(created_jobs) == 1
    assert created_jobs[0].metadata["type"] == job_data_created.metadata["type"]

    assert len(in_progress_jobs) == 1
    assert in_progress_jobs[0].metadata["type"] == job_data_in_progress.metadata["type"]

    assert len(completed_jobs) == 1
    assert completed_jobs[0].metadata["type"] == job_data_completed.metadata["type"]


def test_list_jobs_filter_by_type(server: SyncServer, default_user, default_job):
    """Test that list_jobs correctly filters by job_type."""
    # Create a run job
    run_pydantic = PydanticJob(
        user_id=default_user.id,
        status=JobStatus.pending,
        job_type=JobType.RUN,
    )
    run = server.job_manager.create_job(pydantic_job=run_pydantic, actor=default_user)

    # List only regular jobs
    jobs = server.job_manager.list_jobs(actor=default_user)
    assert len(jobs) == 1
    assert jobs[0].id == default_job.id

    # List only run jobs
    jobs = server.job_manager.list_jobs(actor=default_user, job_type=JobType.RUN)
    assert len(jobs) == 1
    assert jobs[0].id == run.id


def test_e2e_job_callback(monkeypatch, server: SyncServer, default_user):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json

        class FakeResponse:
            status_code = 202

        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    job_in = PydanticJob(status=JobStatus.created, metadata={"foo": "bar"}, callback_url="http://example.test/webhook/jobs")
    created = server.job_manager.create_job(job_in, actor=default_user)
    assert created.callback_url == "http://example.test/webhook/jobs"

    update = JobUpdate(status=JobStatus.completed)
    updated = server.job_manager.update_job_by_id(created.id, update, actor=default_user)

    assert captured["url"] == created.callback_url
    assert captured["json"]["job_id"] == created.id
    assert captured["json"]["status"] == JobStatus.completed.value

    # Normalize the received completed_at to compare properly
    actual_dt = datetime.fromisoformat(captured["json"]["completed_at"]).replace(tzinfo=None)
    expected_dt = updated.completed_at.replace(tzinfo=None)
    assert actual_dt == expected_dt

    assert isinstance(updated.callback_sent_at, datetime)
    assert updated.callback_status_code == 202


# ======================================================================================================================
# JobManager Tests - Messages
# ======================================================================================================================


def test_job_messages_add(server: SyncServer, default_run, hello_world_message_fixture, default_user):
    """Test adding a message to a job."""
    # Add message to job
    server.job_manager.add_message_to_job(
        job_id=default_run.id,
        message_id=hello_world_message_fixture.id,
        actor=default_user,
    )

    # Verify message was added
    messages = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
    )
    assert len(messages) == 1
    assert messages[0].id == hello_world_message_fixture.id
    assert messages[0].content[0].text == hello_world_message_fixture.content[0].text


def test_job_messages_pagination(server: SyncServer, default_run, default_user, sarah_agent):
    """Test pagination of job messages."""
    # Create multiple messages
    message_ids = []
    for i in range(5):
        message = PydanticMessage(
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
            role=MessageRole.user,
            content=[TextContent(text=f"Test message {i}")],
        )
        msg = server.message_manager.create_message(message, actor=default_user)
        message_ids.append(msg.id)

        # Add message to job
        server.job_manager.add_message_to_job(
            job_id=default_run.id,
            message_id=msg.id,
            actor=default_user,
        )

    # Test pagination with limit
    messages = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        limit=2,
    )
    assert len(messages) == 2
    assert messages[0].id == message_ids[0]
    assert messages[1].id == message_ids[1]

    # Test pagination with cursor
    first_page = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        limit=2,
        ascending=True,  # [M0, M1]
    )
    assert len(first_page) == 2
    assert first_page[0].id == message_ids[0]
    assert first_page[1].id == message_ids[1]
    assert first_page[0].created_at <= first_page[1].created_at

    last_page = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        limit=2,
        ascending=False,  # [M4, M3]
    )
    assert len(last_page) == 2
    assert last_page[0].id == message_ids[4]
    assert last_page[1].id == message_ids[3]
    assert last_page[0].created_at >= last_page[1].created_at

    first_page_ids = set(msg.id for msg in first_page)
    last_page_ids = set(msg.id for msg in last_page)
    assert first_page_ids.isdisjoint(last_page_ids)

    # Test middle page using both before and after
    middle_page = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        before=last_page[-1].id,  # M3
        after=first_page[0].id,  # M0
        ascending=True,  # [M1, M2]
    )
    assert len(middle_page) == 2  # Should include message between first and last pages
    assert middle_page[0].id == message_ids[1]
    assert middle_page[1].id == message_ids[2]
    head_tail_msgs = first_page_ids.union(last_page_ids)
    assert middle_page[1].id not in head_tail_msgs
    assert middle_page[0].id in first_page_ids

    # Test descending order for middle page
    middle_page = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        before=last_page[-1].id,  # M3
        after=first_page[0].id,  # M0
        ascending=False,  # [M2, M1]
    )
    assert len(middle_page) == 2  # Should include message between first and last pages
    assert middle_page[0].id == message_ids[2]
    assert middle_page[1].id == message_ids[1]

    # Test getting earliest messages
    msg_3 = last_page[-1].id
    earliest_msgs = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        ascending=False,
        before=msg_3,  # Get messages after M3 in descending order
    )
    assert len(earliest_msgs) == 3  # Should get M2, M1, M0
    assert all(m.id not in last_page_ids for m in earliest_msgs)
    assert earliest_msgs[0].created_at > earliest_msgs[1].created_at > earliest_msgs[2].created_at

    # Test getting earliest messages with ascending order
    earliest_msgs_ascending = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        ascending=True,
        before=msg_3,  # Get messages before M3 in ascending order
    )
    assert len(earliest_msgs_ascending) == 3  # Should get M0, M1, M2
    assert all(m.id not in last_page_ids for m in earliest_msgs_ascending)
    assert earliest_msgs_ascending[0].created_at < earliest_msgs_ascending[1].created_at < earliest_msgs_ascending[2].created_at


def test_job_messages_ordering(server: SyncServer, default_run, default_user, sarah_agent):
    """Test that messages are ordered by created_at."""
    # Create messages with different timestamps
    base_time = datetime.now(timezone.utc)
    message_times = [
        base_time - timedelta(minutes=2),
        base_time - timedelta(minutes=1),
        base_time,
    ]

    for i, created_at in enumerate(message_times):
        message = PydanticMessage(
            role=MessageRole.user,
            content=[TextContent(text="Test message")],
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
            created_at=created_at,
        )
        msg = server.message_manager.create_message(message, actor=default_user)

        # Add message to job
        server.job_manager.add_message_to_job(
            job_id=default_run.id,
            message_id=msg.id,
            actor=default_user,
        )

    # Verify messages are returned in chronological order
    returned_messages = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
    )

    assert len(returned_messages) == 3
    assert returned_messages[0].created_at < returned_messages[1].created_at
    assert returned_messages[1].created_at < returned_messages[2].created_at

    # Verify messages are returned in descending order
    returned_messages = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
        ascending=False,
    )

    assert len(returned_messages) == 3
    assert returned_messages[0].created_at > returned_messages[1].created_at
    assert returned_messages[1].created_at > returned_messages[2].created_at


def test_job_messages_empty(server: SyncServer, default_run, default_user):
    """Test getting messages for a job with no messages."""
    messages = server.job_manager.get_job_messages(
        job_id=default_run.id,
        actor=default_user,
    )
    assert len(messages) == 0


def test_job_messages_add_duplicate(server: SyncServer, default_run, hello_world_message_fixture, default_user):
    """Test adding the same message to a job twice."""
    # Add message to job first time
    server.job_manager.add_message_to_job(
        job_id=default_run.id,
        message_id=hello_world_message_fixture.id,
        actor=default_user,
    )

    # Attempt to add same message again
    with pytest.raises(IntegrityError):
        server.job_manager.add_message_to_job(
            job_id=default_run.id,
            message_id=hello_world_message_fixture.id,
            actor=default_user,
        )


def test_job_messages_filter(server: SyncServer, default_run, default_user, sarah_agent):
    """Test getting messages associated with a job."""
    # Create test messages with different roles and tool calls
    messages = [
        PydanticMessage(
            role=MessageRole.user,
            content=[TextContent(text="Hello")],
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
        ),
        PydanticMessage(
            role=MessageRole.assistant,
            content=[TextContent(text="Hi there!")],
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
        ),
        PydanticMessage(
            role=MessageRole.assistant,
            content=[TextContent(text="Let me help you with that")],
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
            tool_calls=[
                OpenAIToolCall(
                    id="call_1",
                    type="function",
                    function=OpenAIFunction(
                        name="test_tool",
                        arguments='{"arg1": "value1"}',
                    ),
                )
            ],
        ),
    ]

    # Add messages to job
    for msg in messages:
        created_msg = server.message_manager.create_message(msg, actor=default_user)
        server.job_manager.add_message_to_job(default_run.id, created_msg.id, actor=default_user)

    # Test getting all messages
    all_messages = server.job_manager.get_job_messages(job_id=default_run.id, actor=default_user)
    assert len(all_messages) == 3

    # Test filtering by role
    user_messages = server.job_manager.get_job_messages(job_id=default_run.id, actor=default_user, role=MessageRole.user)
    assert len(user_messages) == 1
    assert user_messages[0].role == MessageRole.user

    # Test limit
    limited_messages = server.job_manager.get_job_messages(job_id=default_run.id, actor=default_user, limit=2)
    assert len(limited_messages) == 2


def test_get_run_messages(server: SyncServer, default_user: PydanticUser, sarah_agent):
    """Test getting messages for a run with request config."""
    # Create a run with custom request config
    run = server.job_manager.create_job(
        pydantic_job=PydanticRun(
            user_id=default_user.id,
            status=JobStatus.created,
            request_config=LettaRequestConfig(
                use_assistant_message=False, assistant_message_tool_name="custom_tool", assistant_message_tool_kwarg="custom_arg"
            ),
        ),
        actor=default_user,
    )

    # Add some messages
    messages = [
        PydanticMessage(
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
            role=MessageRole.tool if i % 2 == 0 else MessageRole.assistant,
            content=[TextContent(text=f"Test message {i}" if i % 2 == 1 else '{"status": "OK"}')],
            tool_calls=(
                [{"type": "function", "id": f"call_{i//2}", "function": {"name": "custom_tool", "arguments": '{"custom_arg": "test"}'}}]
                if i % 2 == 1
                else None
            ),
            tool_call_id=f"call_{i//2}" if i % 2 == 0 else None,
        )
        for i in range(4)
    ]

    for msg in messages:
        created_msg = server.message_manager.create_message(msg, actor=default_user)
        server.job_manager.add_message_to_job(job_id=run.id, message_id=created_msg.id, actor=default_user)

    # Get messages and verify they're converted correctly
    result = server.job_manager.get_run_messages(run_id=run.id, actor=default_user)

    # Verify correct number of messages. Assistant messages should be parsed
    assert len(result) == 6

    # Verify assistant messages are parsed according to request config
    tool_call_messages = [msg for msg in result if msg.message_type == "tool_call_message"]
    reasoning_messages = [msg for msg in result if msg.message_type == "reasoning_message"]
    assert len(tool_call_messages) == 2
    assert len(reasoning_messages) == 2
    for msg in tool_call_messages:
        assert msg.tool_call is not None
        assert msg.tool_call.name == "custom_tool"


def test_get_run_messages(server: SyncServer, default_user: PydanticUser, sarah_agent):
    """Test getting messages for a run with request config."""
    # Create a run with custom request config
    run = server.job_manager.create_job(
        pydantic_job=PydanticRun(
            user_id=default_user.id,
            status=JobStatus.created,
            request_config=LettaRequestConfig(
                use_assistant_message=True, assistant_message_tool_name="custom_tool", assistant_message_tool_kwarg="custom_arg"
            ),
        ),
        actor=default_user,
    )

    # Add some messages
    messages = [
        PydanticMessage(
            organization_id=default_user.organization_id,
            agent_id=sarah_agent.id,
            role=MessageRole.tool if i % 2 == 0 else MessageRole.assistant,
            content=[TextContent(text=f"Test message {i}" if i % 2 == 1 else '{"status": "OK"}')],
            tool_calls=(
                [{"type": "function", "id": f"call_{i//2}", "function": {"name": "custom_tool", "arguments": '{"custom_arg": "test"}'}}]
                if i % 2 == 1
                else None
            ),
            tool_call_id=f"call_{i//2}" if i % 2 == 0 else None,
        )
        for i in range(4)
    ]

    for msg in messages:
        created_msg = server.message_manager.create_message(msg, actor=default_user)
        server.job_manager.add_message_to_job(job_id=run.id, message_id=created_msg.id, actor=default_user)

    # Get messages and verify they're converted correctly
    result = server.job_manager.get_run_messages(run_id=run.id, actor=default_user)

    # Verify correct number of messages. Assistant messages should be parsed
    assert len(result) == 4

    # Verify assistant messages are parsed according to request config
    assistant_messages = [msg for msg in result if msg.message_type == "assistant_message"]
    reasoning_messages = [msg for msg in result if msg.message_type == "reasoning_message"]
    assert len(assistant_messages) == 2
    assert len(reasoning_messages) == 2
    for msg in assistant_messages:
        assert msg.content == "test"
    for msg in reasoning_messages:
        assert "Test message" in msg.reasoning


# ======================================================================================================================
# JobManager Tests - Usage Statistics
# ======================================================================================================================


def test_job_usage_stats_add_and_get(server: SyncServer, sarah_agent, default_job, default_user):
    """Test adding and retrieving job usage statistics."""
    job_manager = server.job_manager
    step_manager = server.step_manager

    # Add usage statistics
    step_manager.log_step(
        agent_id=sarah_agent.id,
        provider_name="openai",
        model="gpt-4",
        model_endpoint="https://api.openai.com/v1",
        context_window_limit=8192,
        job_id=default_job.id,
        usage=UsageStatistics(
            completion_tokens=100,
            prompt_tokens=50,
            total_tokens=150,
        ),
        actor=default_user,
    )

    # Get usage statistics
    usage_stats = job_manager.get_job_usage(job_id=default_job.id, actor=default_user)

    # Verify the statistics
    assert usage_stats.completion_tokens == 100
    assert usage_stats.prompt_tokens == 50
    assert usage_stats.total_tokens == 150

    # get steps
    steps = job_manager.get_job_steps(job_id=default_job.id, actor=default_user)
    assert len(steps) == 1


def test_job_usage_stats_get_no_stats(server: SyncServer, default_job, default_user):
    """Test getting usage statistics for a job with no stats."""
    job_manager = server.job_manager

    # Get usage statistics for a job with no stats
    usage_stats = job_manager.get_job_usage(job_id=default_job.id, actor=default_user)

    # Verify default values
    assert usage_stats.completion_tokens == 0
    assert usage_stats.prompt_tokens == 0
    assert usage_stats.total_tokens == 0

    # get steps
    steps = job_manager.get_job_steps(job_id=default_job.id, actor=default_user)
    assert len(steps) == 0


def test_job_usage_stats_add_multiple(server: SyncServer, sarah_agent, default_job, default_user):
    """Test adding multiple usage statistics entries for a job."""
    job_manager = server.job_manager
    step_manager = server.step_manager

    # Add first usage statistics entry
    step_manager.log_step(
        agent_id=sarah_agent.id,
        provider_name="openai",
        model="gpt-4",
        model_endpoint="https://api.openai.com/v1",
        context_window_limit=8192,
        job_id=default_job.id,
        usage=UsageStatistics(
            completion_tokens=100,
            prompt_tokens=50,
            total_tokens=150,
        ),
        actor=default_user,
    )

    # Add second usage statistics entry
    step_manager.log_step(
        agent_id=sarah_agent.id,
        provider_name="openai",
        model="gpt-4",
        model_endpoint="https://api.openai.com/v1",
        context_window_limit=8192,
        job_id=default_job.id,
        usage=UsageStatistics(
            completion_tokens=200,
            prompt_tokens=100,
            total_tokens=300,
        ),
        actor=default_user,
    )

    # Get usage statistics (should return the latest entry)
    usage_stats = job_manager.get_job_usage(job_id=default_job.id, actor=default_user)

    # Verify we get the most recent statistics
    assert usage_stats.completion_tokens == 300
    assert usage_stats.prompt_tokens == 150
    assert usage_stats.total_tokens == 450
    assert usage_stats.step_count == 2

    # get steps
    steps = job_manager.get_job_steps(job_id=default_job.id, actor=default_user)
    assert len(steps) == 2

    # get agent steps
    steps = step_manager.list_steps(agent_id=sarah_agent.id, actor=default_user)
    assert len(steps) == 2


def test_job_usage_stats_get_nonexistent_job(server: SyncServer, default_user):
    """Test getting usage statistics for a nonexistent job."""
    job_manager = server.job_manager

    with pytest.raises(NoResultFound):
        job_manager.get_job_usage(job_id="nonexistent_job", actor=default_user)


def test_job_usage_stats_add_nonexistent_job(server: SyncServer, sarah_agent, default_user):
    """Test adding usage statistics for a nonexistent job."""
    step_manager = server.step_manager

    with pytest.raises(NoResultFound):
        step_manager.log_step(
            agent_id=sarah_agent.id,
            provider_name="openai",
            model="gpt-4",
            model_endpoint="https://api.openai.com/v1",
            context_window_limit=8192,
            job_id="nonexistent_job",
            usage=UsageStatistics(
                completion_tokens=100,
                prompt_tokens=50,
                total_tokens=150,
            ),
            actor=default_user,
        )


def test_list_tags(server: SyncServer, default_user, default_organization):
    """Test listing tags functionality."""
    # Create multiple agents with different tags
    agents = []
    tags = ["alpha", "beta", "gamma", "delta", "epsilon"]

    # Create agents with different combinations of tags
    for i in range(3):
        agent = server.agent_manager.create_agent(
            actor=default_user,
            agent_create=CreateAgent(
                name="tag_agent_" + str(i),
                memory_blocks=[],
                llm_config=LLMConfig.default_config("gpt-4"),
                embedding_config=EmbeddingConfig.default_config(provider="openai"),
                tags=tags[i : i + 3],  # Each agent gets 3 consecutive tags
                include_base_tools=False,
            ),
        )
        agents.append(agent)

    # Test basic listing - should return all unique tags in alphabetical order
    all_tags = server.agent_manager.list_tags(actor=default_user)
    assert all_tags == sorted(tags[:5])  # All tags should be present and sorted

    # Test pagination with limit
    limited_tags = server.agent_manager.list_tags(actor=default_user, limit=2)
    assert limited_tags == tags[:2]  # Should return first 2 tags

    # Test pagination with cursor
    cursor_tags = server.agent_manager.list_tags(actor=default_user, after="beta")
    assert cursor_tags == ["delta", "epsilon", "gamma"]  # Tags after "beta"

    # Test text search
    search_tags = server.agent_manager.list_tags(actor=default_user, query_text="ta")
    assert search_tags == ["beta", "delta"]  # Only tags containing "ta"

    # Test with non-matching search
    no_match_tags = server.agent_manager.list_tags(actor=default_user, query_text="xyz")
    assert no_match_tags == []  # Should return empty list

    # Test with different organization
    other_org = server.organization_manager.create_organization(pydantic_org=PydanticOrganization(name="Other Org"))
    other_user = server.user_manager.create_user(PydanticUser(name="Other User", organization_id=other_org.id))

    # Other org's tags should be empty
    other_org_tags = server.agent_manager.list_tags(actor=other_user)
    assert other_org_tags == []

    # Cleanup
    for agent in agents:
        server.agent_manager.delete_agent(agent.id, actor=default_user)


# ======================================================================================================================
# LLMBatchManager Tests
# ======================================================================================================================


def test_create_and_get_batch_request(server, default_user, dummy_beta_message_batch, letta_batch_job):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )
    assert batch.id.startswith("batch_req-")
    assert batch.create_batch_response == dummy_beta_message_batch
    fetched = server.batch_manager.get_llm_batch_job_by_id(batch.id, actor=default_user)
    assert fetched.id == batch.id


def test_update_batch_status(server, default_user, dummy_beta_message_batch, letta_batch_job):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )
    before = datetime.now(timezone.utc)

    server.batch_manager.update_llm_batch_status(
        llm_batch_id=batch.id,
        status=JobStatus.completed,
        latest_polling_response=dummy_beta_message_batch,
        actor=default_user,
    )

    updated = server.batch_manager.get_llm_batch_job_by_id(batch.id, actor=default_user)
    assert updated.status == JobStatus.completed
    assert updated.latest_polling_response == dummy_beta_message_batch
    assert updated.last_polled_at >= before


def test_create_and_get_batch_item(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    assert item.llm_batch_id == batch.id
    assert item.agent_id == sarah_agent.id
    assert item.step_state == dummy_step_state

    fetched = server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)
    assert fetched.id == item.id


def test_update_batch_item(
    server,
    default_user,
    sarah_agent,
    dummy_beta_message_batch,
    dummy_llm_config,
    dummy_step_state,
    dummy_successful_response,
    letta_batch_job,
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    updated_step_state = AgentStepState(step_number=2, tool_rules_solver=dummy_step_state.tool_rules_solver)

    server.batch_manager.update_llm_batch_item(
        item_id=item.id,
        request_status=JobStatus.completed,
        step_status=AgentStepStatus.resumed,
        llm_request_response=dummy_successful_response,
        step_state=updated_step_state,
        actor=default_user,
    )

    updated = server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)
    assert updated.request_status == JobStatus.completed
    assert updated.batch_request_result == dummy_successful_response


def test_delete_batch_item(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    server.batch_manager.delete_llm_batch_item(item_id=item.id, actor=default_user)

    with pytest.raises(NoResultFound):
        server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)


def test_list_running_batches(server, default_user, dummy_beta_message_batch, letta_batch_job):
    server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.running,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    running_batches = server.batch_manager.list_running_llm_batches(actor=default_user)
    assert len(running_batches) >= 1
    assert all(batch.status == JobStatus.running for batch in running_batches)


def test_bulk_update_batch_statuses(server, default_user, dummy_beta_message_batch, letta_batch_job):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    server.batch_manager.bulk_update_llm_batch_statuses([(batch.id, JobStatus.completed, dummy_beta_message_batch)])

    updated = server.batch_manager.get_llm_batch_job_by_id(batch.id, actor=default_user)
    assert updated.status == JobStatus.completed
    assert updated.latest_polling_response == dummy_beta_message_batch


def test_bulk_update_batch_items_results_by_agent(
    server,
    default_user,
    sarah_agent,
    dummy_beta_message_batch,
    dummy_llm_config,
    dummy_step_state,
    dummy_successful_response,
    letta_batch_job,
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )
    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    server.batch_manager.bulk_update_batch_llm_items_results_by_agent(
        [ItemUpdateInfo(batch.id, sarah_agent.id, JobStatus.completed, dummy_successful_response)]
    )

    updated = server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)
    assert updated.request_status == JobStatus.completed
    assert updated.batch_request_result == dummy_successful_response


def test_bulk_update_batch_items_step_status_by_agent(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )
    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    server.batch_manager.bulk_update_llm_batch_items_step_status_by_agent(
        [StepStatusUpdateInfo(batch.id, sarah_agent.id, AgentStepStatus.resumed)]
    )

    updated = server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)
    assert updated.step_status == AgentStepStatus.resumed


def test_list_batch_items_limit_and_filter(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    for _ in range(3):
        server.batch_manager.create_llm_batch_item(
            llm_batch_id=batch.id,
            agent_id=sarah_agent.id,
            llm_config=dummy_llm_config,
            step_state=dummy_step_state,
            actor=default_user,
        )

    all_items = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, actor=default_user)
    limited_items = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, limit=2, actor=default_user)

    assert len(all_items) >= 3
    assert len(limited_items) == 2


def test_list_batch_items_pagination(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    # Create a batch job.
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    # Create 10 batch items.
    created_items = []
    for i in range(10):
        item = server.batch_manager.create_llm_batch_item(
            llm_batch_id=batch.id,
            agent_id=sarah_agent.id,
            llm_config=dummy_llm_config,
            step_state=dummy_step_state,
            actor=default_user,
        )
        created_items.append(item)

    # Retrieve all items (without pagination).
    all_items = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, actor=default_user)
    assert len(all_items) >= 10, f"Expected at least 10 items, got {len(all_items)}"

    # Verify the items are ordered ascending by id (based on our implementation).
    sorted_ids = [item.id for item in sorted(all_items, key=lambda i: i.id)]
    retrieved_ids = [item.id for item in all_items]
    assert retrieved_ids == sorted_ids, "Batch items are not ordered in ascending order by id"

    # Choose a cursor: the id of the 5th item.
    cursor = all_items[4].id

    # Retrieve items after the cursor.
    paged_items = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, actor=default_user, after=cursor)

    # All returned items should have an id greater than the cursor.
    for item in paged_items:
        assert item.id > cursor, f"Item id {item.id} is not greater than the cursor {cursor}"

    # Count expected remaining items.
    # Find the index of the cursor in our sorted list.
    cursor_index = sorted_ids.index(cursor)
    expected_remaining = len(sorted_ids) - cursor_index - 1
    assert len(paged_items) == expected_remaining, f"Expected {expected_remaining} items after cursor, got {len(paged_items)}"

    # Test pagination with a limit.
    limit = 3
    limited_page = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, actor=default_user, after=cursor, limit=limit)
    # If more than 'limit' items remain, we should only get exactly 'limit' items.
    assert len(limited_page) == min(
        limit, expected_remaining
    ), f"Expected {min(limit, expected_remaining)} items with limit {limit}, got {len(limited_page)}"

    # Optional: Test with a cursor beyond the last item returns an empty list.
    last_cursor = sorted_ids[-1]
    empty_page = server.batch_manager.list_llm_batch_items(llm_batch_id=batch.id, actor=default_user, after=last_cursor)
    assert empty_page == [], "Expected an empty list when cursor is after the last item"


def test_bulk_update_batch_items_request_status_by_agent(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    # Create a batch job
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    # Create a batch item
    item = server.batch_manager.create_llm_batch_item(
        llm_batch_id=batch.id,
        agent_id=sarah_agent.id,
        llm_config=dummy_llm_config,
        step_state=dummy_step_state,
        actor=default_user,
    )

    # Update the request status using the bulk update method
    server.batch_manager.bulk_update_llm_batch_items_request_status_by_agent(
        [RequestStatusUpdateInfo(batch.id, sarah_agent.id, JobStatus.expired)]
    )

    # Verify the update was applied
    updated = server.batch_manager.get_llm_batch_item_by_id(item.id, actor=default_user)
    assert updated.request_status == JobStatus.expired


def test_bulk_update_nonexistent_items(server, default_user, dummy_beta_message_batch, dummy_successful_response, letta_batch_job):
    # Create a batch job
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    # Attempt to update non-existent items should not raise errors

    # Test with the direct bulk_update_llm_batch_items method
    nonexistent_pairs = [(batch.id, "nonexistent-agent-id")]
    nonexistent_updates = [{"request_status": JobStatus.expired}]

    # This should not raise an error, just silently skip non-existent items
    server.batch_manager.bulk_update_llm_batch_items(nonexistent_pairs, nonexistent_updates)

    # Test with higher-level methods
    # Results by agent
    server.batch_manager.bulk_update_batch_llm_items_results_by_agent(
        [ItemUpdateInfo(batch.id, "nonexistent-agent-id", JobStatus.expired, dummy_successful_response)]
    )

    # Step status by agent
    server.batch_manager.bulk_update_llm_batch_items_step_status_by_agent(
        [StepStatusUpdateInfo(batch.id, "nonexistent-agent-id", AgentStepStatus.resumed)]
    )

    # Request status by agent
    server.batch_manager.bulk_update_llm_batch_items_request_status_by_agent(
        [RequestStatusUpdateInfo(batch.id, "nonexistent-agent-id", JobStatus.expired)]
    )


def test_create_batch_items_bulk(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    # Create a batch job
    llm_batch_job = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    # Prepare data for multiple batch items
    batch_items = []
    agent_ids = [sarah_agent.id, sarah_agent.id, sarah_agent.id]  # Using the same agent for simplicity

    for agent_id in agent_ids:
        batch_item = LLMBatchItem(
            llm_batch_id=llm_batch_job.id,
            agent_id=agent_id,
            llm_config=dummy_llm_config,
            request_status=JobStatus.created,
            step_status=AgentStepStatus.paused,
            step_state=dummy_step_state,
        )
        batch_items.append(batch_item)

    # Call the bulk create function
    created_items = server.batch_manager.create_llm_batch_items_bulk(batch_items, actor=default_user)

    # Verify the correct number of items were created
    assert len(created_items) == len(agent_ids)

    # Verify each item has expected properties
    for item in created_items:
        assert item.id.startswith("batch_item-")
        assert item.llm_batch_id == llm_batch_job.id
        assert item.agent_id in agent_ids
        assert item.llm_config == dummy_llm_config
        assert item.request_status == JobStatus.created
        assert item.step_status == AgentStepStatus.paused
        assert item.step_state == dummy_step_state

    # Verify items can be retrieved from the database
    all_items = server.batch_manager.list_llm_batch_items(llm_batch_id=llm_batch_job.id, actor=default_user)
    assert len(all_items) >= len(agent_ids)

    # Verify the IDs of created items match what's in the database
    created_ids = [item.id for item in created_items]
    for item_id in created_ids:
        fetched = server.batch_manager.get_llm_batch_item_by_id(item_id, actor=default_user)
        assert fetched.id in created_ids


def test_count_batch_items(
    server, default_user, sarah_agent, dummy_beta_message_batch, dummy_llm_config, dummy_step_state, letta_batch_job
):
    # Create a batch job first.
    batch = server.batch_manager.create_llm_batch_job(
        llm_provider=ProviderType.anthropic,
        status=JobStatus.created,
        create_batch_response=dummy_beta_message_batch,
        actor=default_user,
        letta_batch_job_id=letta_batch_job.id,
    )

    # Create a specific number of batch items for this batch.
    num_items = 5
    for _ in range(num_items):
        server.batch_manager.create_llm_batch_item(
            llm_batch_id=batch.id,
            agent_id=sarah_agent.id,
            llm_config=dummy_llm_config,
            step_state=dummy_step_state,
            actor=default_user,
        )

    # Use the count_llm_batch_items method to count the items.
    count = server.batch_manager.count_llm_batch_items(llm_batch_id=batch.id)

    # Assert that the count matches the expected number.
    assert count == num_items, f"Expected {num_items} items, got {count}"
