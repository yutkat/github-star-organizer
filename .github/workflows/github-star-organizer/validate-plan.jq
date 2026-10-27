# Agent-side mirror of apply.py validate_plan, so the agent needs only jq.
# The input self-digest is not recomputed here; the post-step still verifies it.
$input[0] as $in
| $plan[0] as $p
| ($in.existing_lists | map(.id)) as $lists
| ($in.repositories | map(.id)) as $required
| (if ($p.assignments | type) == "array" then $p.assignments else [] end) as $entries
| ($entries | map(select(type == "object"))) as $assignments
| ($assignments | map(.repository_id)) as $assigned
| [
    if ($p.source_sha256 | type) != "string" then
      "plan source_sha256 is required"
    elif $p.source_sha256 != $in.source_sha256 then
      "plan does not match the categorization input"
    else empty end,
    if ($p.new_lists // []) != [] then
      "creating lists is not supported; assign existing lists only"
    else empty end,
    if ($p.assignments | type) != "array" then "assignments must be an array"
    else empty end,
    if ($entries | length) != ($assignments | length) then
      "assignments entries must be objects"
    else empty end,
    ($assigned | group_by(.) | map(select(length > 1)[0]) | .[]
      | "duplicate assignment for \(.)"),
    ($assignments | map(.list_ref) | unique - $lists | .[]
      | "unknown list reference: \(tojson)"),
    if ($assigned | unique) != ($required | unique) then
      "assignment mismatch; missing=\($required - $assigned | unique | tojson), extra=\($assigned - $required | unique | tojson)"
    else empty end
  ] as $errors
| if $errors == [] then
    {
      assignments: ($assigned | length),
      assignments_by_list: ($assignments | group_by(.list_ref)
        | map({key: .[0].list_ref, value: length}) | from_entries)
    }
  else
    $errors | join("\n") + "\n" | halt_error(1)
  end
