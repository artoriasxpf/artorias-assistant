"""Select registered workspace tools for a planned step. Authorization stays at execution."""
READ_TOOLS = frozenset({'project_tree', 'project_list', 'project_read', 'project_inventory',
                        'project_search', 'project_read_many'})
WRITE_TOOLS = frozenset({'project_write', 'project_edit', 'project_delete', 'project_move',
                         'project_mkdir', 'project_change_apply'})
EXECUTION_TOOLS = frozenset({'run_python'})


def step_tools(tools, groups, analysis):
    if groups is None and not analysis:
        return tools
    groups = set(groups or ())
    allowed = set()
    if analysis or 'project_read' in groups:
        allowed.update(READ_TOOLS | {'project_change_plan'})
    if 'project_write' in groups:
        allowed.update(WRITE_TOOLS)
    if 'execution' in groups:
        allowed.update(EXECUTION_TOOLS)
    return [item for item in tools if item.name in allowed]
