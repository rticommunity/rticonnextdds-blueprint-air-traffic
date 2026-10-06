# Design Process

This directory records how the Air Traffic Control simulation was built with RTI Connext DDS. It is a learning resource that shows how AI tools, with and without the Connext MCP server, were used to design and implement the DDS architecture.

## Reading Order

1. **[high_level_scenario.md](high_level_scenario.md)**: the original scenario description that started the project
2. **[architecture_overview.md](architecture_overview.md)**: the first, technology-agnostic architecture sketch
3. **[agent_prompts.md](agent_prompts.md)**: prompts used with AI tools during design
4. **[initial_connext_issues.md](initial_connext_issues.md)**: early issues found in AI-generated Connext DDS code
5. **[nomcp_design_connext_dds_iter1.md](nomcp_design_connext_dds_iter1.md)**: design iteration without MCP
6. **[mcp_design_connext_dds_iter1.md](mcp_design_connext_dds_iter1.md)**: design iteration with MCP
7. **[use_of_connext_mcp.md](use_of_connext_mcp.md)**: what the MCP server changed, compared with designing without it
