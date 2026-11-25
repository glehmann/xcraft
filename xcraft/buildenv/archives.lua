-- Rewrite the member headers of the static archives of a build root as ar's deterministic mode, its D modifier, writes
-- them: time, owner and group 0, mode 644. The same objects then make the same archive, whatever the time and the user
-- of the build.
--
-- Run by rpmbuild at the end of %__spec_install_post, after brp-strip-static-archive, whose strip -g writes the
-- headers again with the time and the user of the build, with rpm's Lua, as the build root has no other interpreter
-- than the shell: rpmlua archives.lua <build root>, rpmlua giving the script as arg[1]. It edits the headers itself,
-- as objcopy -D and strip -D fail on the archives of objects that are not ELF, like those of dev86.

local MAGIC, HEADER = "!<arch>\n", 60

local function pad(text, size)
    return text .. string.rep(" ", size - #text)
end

-- the archive `data` with the headers of its members as ar D writes them, or nil and why it is not an ar archive
local function deterministic(data)
    local parts, pos = { MAGIC }, #MAGIC + 1
    while pos <= #data do
        local header = data:sub(pos, pos + HEADER - 1)
        if #header < HEADER or header:sub(59, 60) ~= "`\n" then
            return nil, "no header of a member at " .. (pos - 1)
        end
        local name = header:sub(1, 16):gsub(" +$", "")
        if name == "/" or name == "/SYM64/" then -- the symbol table: owner, group and mode already 0
            header = header:sub(1, 16) .. pad("0", 12) .. header:sub(29)
        elseif name ~= "//" then -- but the table of the long names, without time, owner, group nor mode
            header = header:sub(1, 16) .. pad("0", 12) .. pad("0", 6) .. pad("0", 6) .. pad("644", 8) .. header:sub(49)
        end
        local size = tonumber(header:sub(49, 58))
        local stop = pos + HEADER + size + size % 2
        parts[#parts + 1] = header .. data:sub(pos + HEADER, stop - 1)
        pos = stop
    end
    return table.concat(parts)
end

local function rewrite(path)
    local f = io.open(path, "rb")
    if not f then
        return
    end
    if f:read(#MAGIC) ~= MAGIC then
        f:close()
        return
    end
    local data = MAGIC .. f:read("a")
    f:close()
    local new, err = deterministic(data)
    if not new then
        io.stderr:write("warning: " .. path .. " not made deterministic: " .. err .. "\n")
        return
    end
    if new == data then
        return
    end
    -- in place, for its hard links, even when read-only, with its times
    local mode, mtime, atime = posix.stat(path, "mode"), posix.stat(path, "mtime"), posix.stat(path, "atime")
    posix.chmod(path, "u+w")
    f = assert(io.open(path, "r+b"))
    f:write(new)
    f:close()
    posix.chmod(path, mode)
    posix.utime(path, mtime, atime)
end

local function walk(directory)
    for _, name in ipairs(posix.dir(directory) or {}) do
        local path = directory .. "/" .. name
        -- not the links: posix.stat follows them
        if name ~= "." and name ~= ".." and not posix.readlink(path) then
            local kind = posix.stat(path, "type")
            if kind == "directory" then
                walk(path)
            elseif kind == "regular" then
                rewrite(path)
            end
        end
    end
end

walk(arg[2])
