'''
Generates Jai bindings for imgui from the JSON emitted by the cimgui project.
'''

OUTPUT_BINDINGS_JAI_FILE = "imgui.jai"
PATH_TO_IMGUI_DLL  = "win\\imgui.dll"
PATH_TO_CIMGUI     = "cimgui"
IMGUI_USE_WCHAR32  = False
SKIP_INTERNAL      = True
STRIP_IMGUI_PREFIX = True # If True, this generator will remove 'ImGui' from the beginning of all identifiers.
                          # note that Im like in `ImVector` remains.

allow_internal = frozenset(["ImDrawListSharedData"])
skip_structs_for_size = frozenset(["ImGuiTextRange", "ImGuiStoragePair"])

import ast
import json
import sys
import operator as op
import os.path
import re
import subprocess
import traceback

from collections import defaultdict, namedtuple
from pprint import pformat, pprint

inline_functions_skipped = []
functions_skipped = []
stats = defaultdict(int)

exports_file = "imgui_exports.txt"
generator_output_dir = f"{PATH_TO_CIMGUI}/generator/output/"

jai_typedefs = dict(
    ImPoolIdx    = "s32",
    ImTextureID  = "*void",
    ImDrawIdx    = "u16",
    ImFileHandle = "*void",
    ImGuiID      = "u32",
    ID           = "u32", # TODO: don't repeat ID like this
)
jai_typedefs_str = "\n".join(f"{key} :: {value};" for key, value in jai_typedefs.items())

extra_code = """
Context :: struct { data: *void; }

ImVector :: struct(T: Type) {
    Size:     s32;
    Capacity: s32;
    Data:     *T;
}

ImPool :: struct(T: Type) {
    Buf: ImVector(T);
    Map: ImGuiStorage;
    FreeIdx: ImPoolIdx;
}

ImChunkStream :: struct(T: Type) {
    Buf: ImVector(s8);
}

<type_definitions>

ImDrawCallback    :: #type (parent_list: *ImDrawList, cmd: *ImDrawCmd) #c_call;
ImGuiInputTextCallback :: #type (data: *ImGuiInputTextCallbackData) -> s32 #c_call;
ImGuiSizeCallback      :: #type (data: *ImGuiSizeCallbackData) #c_call;

ImWchar16 :: u16;
ImWchar32 :: u32;

IMGUI_USE_WCHAR32 :: <IMGUI_USE_WCHAR32>; // TODO: Module parameter

#if IMGUI_USE_WCHAR32
    ImWchar :: ImWchar32;
else
    ImWchar :: ImWchar16;

make_ImVec2 :: inline (a: float, b: float) -> ImVec2 {
    v: ImVec2 = ---;
    v.x = a;
    v.y = b;
    return v;
}

#scope_file

FLT_MAX :: 0h7F7FFFFF;

#if OS == .WINDOWS
    imgui_lib :: #foreign_library "win/imgui";
else
    #assert(false);

""".replace("ImGui", "" if STRIP_IMGUI_PREFIX else "ImGui")\
   .replace("<IMGUI_USE_WCHAR32>", "true" if IMGUI_USE_WCHAR32 else "false")\
   .replace("<type_definitions>", jai_typedefs_str)

# a regex for extracting DLL symbol names from the output of the Microsoft
# Visual Studio command line tool dumpbin.exe
export_line_regex = re.compile(r"(\d+)\s+([\da-fA-F]+)\s+([\da-fA-F]+)\s+([^ ]+) = ([^ ]+) \((.*)\)$")

ctx = dict()

type_replacements = [
    ("char const* *", "**u8"),
    ("unsigned char**", "**u8"),
    ("char const*", "*u8"),
    ("const char*", "*u8"),
    ("unsigned short", "u16"),
    ("unsigned __int64", "u64"),
    ("short", "s16"),
    ("size_t", "u64"),
    ("signed char", "s8"),
    ("const char", "u8"),
    ("const ", ""),
    ("unsigned short", "u16"),
    ("unsigned int", "u32"),
    ("unsigned char", "u8"),
    ("char", "s8"),
    ("long", "s32"),
    ("double", "float64"),
    ("int", "s32"),

    ("ImS8", "s8"),
    ("ImU8", "u8"),
    ("ImS16", "s16"),
    ("ImU16", "u16"),
    ("ImS32", "s32"),
    ("ImU32", "u32"),
    ("ImS64", "s64"),
    ("ImU64", "u64"),
]

def is_trivial_type_replacement(s):
    for cpp, jai in type_replacements:
        if cpp == s:
            return jai

def replace_types(s):
    for c_type, jai_type in type_replacements:
        s = re.sub(r"\b" + c_type.replace("*", "\\*") + r"\b", jai_type, s)
    return s

# supported operators
operators = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
             ast.Div: op.truediv, ast.Pow: op.pow, ast.BitXor: op.xor,
             ast.USub: op.neg}

def eval_expr(expr):
    """
    >>> eval_expr('2^6')
    4
    >>> eval_expr('2**6')
    64
    >>> eval_expr('1 + 2*3**(4^5) / (6 + -7)')
    -5.0
    """
    try:
        return eval_(ast.parse(expr, mode='eval').body)
    except TypeError:
        print("error parsing expression: " + expr, file=sys.stderr)
        raise

def eval_(node):
    if isinstance(node, ast.Num): # <number>
        return node.n
    elif isinstance(node, ast.BinOp): # <left> <operator> <right>
        return operators[type(node.op)](eval_(node.left), eval_(node.right))
    elif isinstance(node, ast.UnaryOp): # <operator> <operand> e.g., -1
        return operators[type(node.op)](eval_(node.operand))
    else:
        raise TypeError(node)

# for extracting the array size from a field type like "TempBuffer[1024*3+1]"
arr_part_re = re.compile(r"(\w+)(?:\[([^\]]+)\])?")

def size_of_type(type):
    if type == "bool": return 32;
    elif type == "unsigned int": return 32
    else: assert false, str(type)

def get_jai_field(field, field_idx, fields):
    place = ''

    bitfield = field.get("bitfield", None)
    if bitfield is not None:
        bitfield = int(bitfield)
        prev_field = fields[field_idx - 1] if field_idx > 0 else None
        # TODO: flesh this out
        if prev_field is not None and bitfield == 1 and prev_field.get("bitfield", None) == "31":
            place = f"#place {prev_field['name']}; "

    template_type = field.get("template_type", None)
    if template_type is not None:
        expect_ptr = False
        if template_type.endswith("*"):
            expect_ptr = True
            template_type = template_type[:-1]

        postfix = "_" + template_type.replace(" ", "_")
        postfix_ptr = "_" + template_type.replace(" ", "_") + "Ptr"

        is_pointer = False
        if expect_ptr and field['type'].endswith(postfix_ptr):
            container_name = field['type'][:-len(postfix_ptr)] 
            is_pointer = True
        elif not expect_ptr and field['type'].endswith(postfix):
            container_name = field['type'][:-len(postfix)]
            is_pointer = False
        else:
            assert False, f"expected field type {field['type']} to end with '{postfix}' (or Ptr)"

        template_type = ("*" if is_pointer else "") + strip_im_prefixes(template_type)
        field['type'] = f"{container_name}({template_type})"

    jai_type = to_jai_type(field['type'])
    size = field.get("size", -1)
    arr_match = arr_part_re.match(field["name"])

    code_size = None
    if arr_match and arr_match.groups()[1] is not None:
        shortened_name, code_size = arr_match.groups()
        if re.match(r"[a-zA-Z_]+", code_size):
            # May be an enum constant like "ImGuiKey_Count". we'll keep the reference to the actual
            # enum value, for better understanding.
            code_size = strip_im_prefixes(code_size)
            code_size = code_size.replace("_", ".", 1)
        else:
            # It's a string expression for a number. we want to just pass the expression
            # through to jai, since it's valuable information to keep the numeric constant
            # factored out (i.e., like keeping 1024*4 instead of 4096)
            assert eval_expr(code_size) == field["size"]

        jai_type = f"[{code_size}]{jai_type}"
    else:
        shortened_name = field["name"]

    if field["name"].endswith("Fn"):
        # Special case for fields with function pointer types.
        #
        # we already handled it above
        pass
    else:
        jai_type = handle_pointers(jai_type)
        if shortened_name == jai_type.replace("*", ""):
            # Uh oh...jai doesn't let us have the name and the type name be the same.
            # so in this case we'll postfix the field name with an underscore.
            shortened_name = f"{shortened_name}_"

    return place, shortened_name, jai_type

def get_jai_func_ptr(jai_type):
    match = re.match(r"([^\(]+)\(([^\)]+)\)\((.*)\)", jai_type)
    # print("--->", match.groups(), "from --- ", jai_type)

    # Plugin_Deinit_Func      :: #type (ctx: *Context, shutting_down: bool) -> *void #c_call;

    if not match:
        assert False, "did not match func ptr regex: " + jai_type

    ret_type, star, args_str = match.groups()
    assert star == "*"

    jai_args_string = ""

    args_str_split = split_args(args_str)
    for i, arg in enumerate(args_str_split):
        jai_arg = is_trivial_type_replacement(arg)
        if jai_arg is not None:
            arg_type = jai_arg
            arg_name = f"unnamed{i}"
            
        else:
            elems = arg.rsplit(" ", 1)
            if len(elems) == 1:
                arg_type = elems[0]
                arg_name = f"unnamed{i}"
            else:
                assert(len(elems) == 2)
                arg_type, arg_name = elems

        arg_type = to_jai_type(arg_type)

        jai_args_string += f"{arg_name}: {arg_type}"
        if i != len(args_str_split) - 1:
            jai_args_string += ", "


    ret_type = handle_pointers(strip_im_prefixes(replace_types(ret_type)))
    if ret_type == "void":
        ret_type_with_arrow = ""
    else:
        ret_type_with_arrow = f" -> {ret_type}"
    
    return f"({jai_args_string}){ret_type_with_arrow} #c_call"

def handle_pointers(t):
    # turn void** c-style pointer declarations into jai-style **void 

    output_t = ""
    while True:
        t = t.strip()
        if t.endswith("*") or t.endswith("&"):
            output_t += "*"
            t = t[:-1]
        elif t.endswith("[]"):
            output_t += "[]"
            t = t[:-2]
        else:
            output_t += t
            break

    return output_t

def p(*a, **k):
    # a shortcut for printing to an output file
    k["file"] = k.get("file", ctx.get("output_file", None))
    print(*a, **k)

def p_aligned(row_tuples, prefix=''):
    # Prints tabulated data aligned

    if not row_tuples: return

    assert all(len(row) == len(row_tuples[0]) for row in row_tuples)

    max_columns = [0] * len(row_tuples[0])

    for row in row_tuples:
        for column in range(len(row)):
            max_columns[column] = max(len(row[column]), max_columns[column])

    for row in row_tuples:
        for column, elem in enumerate(row):
            p(prefix + elem.ljust(max_columns[column]), end="")
        p("", end="\n") # newline
            
def print_section(name):
    p(f"\n//\n// section: {name}\n//\n")

def get_windows_symbols(dll_filename):
    # use dumpbin to export all the symbols from imgui.dll
    if os.path.isfile(exports_file):
        os.remove(exports_file)
    os.system(f"dumpbin /nologo /exports {dll_filename} > {exports_file}")
    assert(os.path.isfile(exports_file))

    started = False
    count = 0
    symbols = []
    for line in open(exports_file, "r"):
        line = line.strip()
        if not line: continue
        if not started and line.strip().startswith("ordinal"):
            # find the first line describing exports
            started = True

        if not started:
            continue

        match = export_line_regex.match(line)
        if match is None: continue

        symbols.append(SymbolEntry(*match.groups()))

    print(f"matched {len(symbols)} total symbols from {dll_filename}.")
    return symbols

SymbolEntry = namedtuple("SymbolEntry", 'ordinal hint rva name1 name2 demangled')

# struct ImGuiContext * GImGui
# struct ImGuiContext * __cdecl ImGui::CreateContext(struct ImFontAtlas *)

# parses windows demangled symbol names from dumpbin.exe /exports
function_pattern = re.compile(r"""
^                                       # beginning of the string
(?:(?P<visibility>\w+):\ )?             # visibility like "public: "
(?P<retval>.*)                          # return value
(?:\ __cdecl\ )                         # calling convention
(?:(?P<nspace_or_stname>[\w\<\> ]+)::)? # namespace or struct name
(?P<fname>[\w\<\>~=\+\-\\\* ]+)         # function name
\(                                      # arguments in parentheses
    (?P<args>.*)
\)
(?P<const>const)?
$
""", re.VERBOSE)

def split_args(args_str):
    # a complicated example from 
        # void __cdecl ImGui::SetAllocatorFunctions(void * (__cdecl*)(unsigned __int64,void *),void (__cdecl*)(void *,void *),void *)
    # the arguments:
        # void * (__cdecl*)(unsigned __int64,void *),void (__cdecl*)(void *,void *),void *

    args_str = args_str.strip()

    level = 0
    start_index = 0
    args = []
    if not args_str:
        return args

    for i, ch in enumerate(args_str):
        if ch == "(": level += 1
        elif ch == ")": level -= 1
        elif ch == "," and level == 0:
            args.append(args_str[start_index:i])
            start_index = i + 1

    # the rest
    args.append(args_str[start_index:])

    return args

assert(split_args("int foo,const char* bar") == ["int foo", "const char* bar"])
assert(split_args("") == [])
assert(split_args("void * (__cdecl*)(unsigned __int64,void *),void (__cdecl*)(void *,void *),void *") == [
    "void * (__cdecl*)(unsigned __int64,void *)",
    "void (__cdecl*)(void *,void *)",
    "void *",
])

def group_symbols(symbols):
    missed_count = 0

    symbols_grouped_by_function_name = defaultdict(list)

    for symbol in symbols:
        mangled   = symbol.name1.strip()
        demangled = symbol.demangled.strip()

        if "__cdecl" not in demangled:
            print("skipping because no __cdecl:", demangled)
            continue

        # sanity check: we expect a function like thing now
        is_function = "(" in demangled and ")" in demangled
        assert is_function, demangled

        match = function_pattern.match(demangled)
        if match is None:
            print("no match: " + demangled + "")
            missed_count += 1
            continue

        symbol_info = match.groupdict()
        symbol_info.update(
            mangled = mangled,
            demangled = demangled,
            args = [normalize_types(arg) for arg in split_args(symbol_info["args"])],
            retval = normalize_types(symbol_info["retval"]),
        )

        if symbol_info["args"] == ["void"]:
            symbol_info["args"] = []

        assert(symbol_info["fname"])
        symbols_grouped_by_function_name[symbol_info["fname"]].append(symbol_info)

    print(f"missed {missed_count} out of {len(symbols)}")

    return symbols_grouped_by_function_name

def normalize_types(cpp_type):
    # remove 'struct '
    cpp_type = cpp_type.replace("struct ", "")

    # normalize  'Thing *' into 'Thing*'
    cpp_type = re.sub(r"(\w) \*", r"\1*", cpp_type)

    return cpp_type

fn_ptr_matcher = re.compile(r"\((?:__cdecl)?\*(?:\w+)?\)\s*\(")

assert(fn_ptr_matcher.search("foo (*)(bar)"))
assert(fn_ptr_matcher.search("foo(__cdecl*)(bar)"))
assert(fn_ptr_matcher.search("*void(*alloc_func)(int bar)"))

def to_jai_type(cpp_type_string):
    cpp_type_string = cpp_type_string.replace("__cdecl", "") # TODO: probably shouldn't just erase this fact...

    if fn_ptr_matcher.search(cpp_type_string):
        return get_jai_func_ptr(cpp_type_string)

    cpp_type_string = cpp_type_string.replace("char const*", "u8*")
    cpp_type_string = cpp_type_string.replace("const char*", "u8*")
    cpp_type_string = cpp_type_string.replace("const char *", "u8*")

    cpp_type_string = cpp_type_string.replace("const ", "")
    cpp_type_string = cpp_type_string.replace(" const", "")
    cpp_type_string = handle_pointers(strip_im_prefixes(replace_types(cpp_type_string)))

    # in jai we put the array part first
    match = re.match(r"^(.*)\[(\d+)\]$", cpp_type_string)
    if match:
        identifier, array_size = match.groups()
        cpp_type_string = f"[{array_size}]{identifier}"

    return cpp_type_string

def all_jai_types_equivalent(enums, zipped_types):
    idx = 0
    for a, b in zipped_types:
        equiv, reason = jai_types_equivalent(enums, a, b)
        if not equiv:
            assert reason
            return False, reason, idx
        idx += 1
    
    return True, None, -1

def jai_types_equivalent(enums, a, b):
    a = a.strip()
    b = b.strip()

    if a > b:
        a, b = b, a

    for typedef_name, jai_type in jai_typedefs.items():
        if a == typedef_name and b == jai_type:
            return True, None
        if b == typedef_name and a == jai_type:
            return True, None

    if a == "ID" and b == "u32":
        return True, None

    # TODO: use a similar thing with jai_typedefs but for function pointers
    if a == "InputTextCallback" and b == "s32 (__cdecl*)(ImGuiInputTextCallbackData*)":
        return True, None

    def starts_with_pointer_or_array(s):
        if s.startswith("*"): return True
        if re.match("^\[(?:\d+)?\]", s): return True
        return False

    def strip_pointer_or_array(s):
        if s.startswith("*"): return s[1:]

        bracket_idx = s.index("]")
        assert bracket_idx != -1
        return s[bracket_idx + 1:]


    while starts_with_pointer_or_array(a) and starts_with_pointer_or_array(b):
        a, b = strip_pointer_or_array(a), strip_pointer_or_array(b)

    arg_name_re = r"(\w+: )"

    # remove the argument names for our comparison if they are function pointers
    # TODO: this is silly. we already know if they are function pointers somewhere
    # above this code, because we did the conversion.
    did_find_func_ptr_a = False
    did_find_func_ptr_b = False
    if "->" in a or "#c_call" in a:
        did_find_func_ptr_a = True
        a = re.sub(arg_name_re, "", a)
    if "->" in b or "#c_call" in b:
        did_find_func_ptr_b = True
        b = re.sub(arg_name_re, "", b)

    if a == b:
        return True, None

    if did_find_func_ptr_a and did_find_func_ptr_b:
        print("~~~~mismatched fn ptrs:\n", a, "\n", b)

    def is_enum(a):
        if enums is None: return False

        # TODO: this is hacky and bad. we need to store the original c name
        return a in enums or (a + "_") in enums or ("ImGui" + a + "_") in enums

    if (is_enum(a) and b == "s32") or (a == "s32" and is_enum(b)):
        return True, None

    jai_wchar_type = "u32" if IMGUI_USE_WCHAR32 else "u16"

    if a == "ImWchar" and b == jai_wchar_type:
        return True, None
    elif a == "ImWchar16" and b == "u16":
        return True, None
    elif a == "ImWchar32" and b == "u32":
        return True, None

    return False, f"a: {a}, b: {b}"
    

def strip_im_prefixes(name):
    if STRIP_IMGUI_PREFIX and name.startswith("ImGui"): name = name[5:]
    #if name.startswith("Im"): name = name[2:]
    return name


def convert_enum_default(enums, val):
    # ImDrawCornerFlags_All -> .All
    #
    # but only if it's a valid enum

    if "_" in val:
        i = val.index("_")
        enum_part = val[:i+1]
        if enum_part in enums:
            return "." + val[i+1:]

    return val

def get_enum_name(enums, jai_enum_name, value):
    jai_enum_name = jai_enum_name + "_"
    enum_entry = enums.get(jai_enum_name, None)
    if enum_entry is None:
        return None

    for val_entry in enum_entry:
        if val_entry["value"] == value:
            name = val_entry["name"]
            assert name.startswith(jai_enum_name), name
            name = name[len(jai_enum_name):]
            return name
    
    return None

def load_structs_and_enums():
    return json.load(open(generator_output_dir + "structs_and_enums.json", "r"))

ArgInfo = namedtuple("ArgInfo", "name jai_arg_type default_str wrapper_arg_type call_arg_value")

def get_jai_args(structs_and_enums, func_entry):
    orig_args = split_args(func_entry["argsoriginal"])
    arg_infos = []
    needs_defaults_wrapper = False
    for i, arg in enumerate(func_entry["argsT"]):
        name     = arg["name"]
        arg_type = arg["type"]

        if name == "...":
            assert arg_type == "..."
            name = "args"
            arg_type = "..Any"

        default_str = ""
        if func_entry["defaults"] == []:
            # definitions.json has [] for the defualts field when
            # it's empty (instead of what you would expect, a {})
            optional_default = None
        else:
            optional_default = func_entry['defaults'].get(name, None)

        jai_arg_type = to_jai_type(arg_type)
        wrapper_arg_type = jai_arg_type
        call_arg_value = name

        if optional_default is not None:
            if optional_default == "((void*)0)":
                optional_default = "null"

            # TODO: check argtype and probably don't use a regex here.
            # do try: float() except ValueError: instead.
            float_match = re.match(r"[+-]?(\d+\.\d+)f", optional_default)
            if float_match is not None:
                # In jai, floating point values do not end with f -- the compiler
                # figures out which type the constant should be for us.
                optional_default = float_match.group(1)

            # hack: also strip f for scientific notation. this should be merged
            # with the code above.
            if re.match(r".*e[+-]\d+[Ff]$", optional_default):
                optional_default = optional_default[:-1]
            if optional_default == "0.0":
                optional_default = "0"


            optional_default = optional_default.replace("(ImU32)", "cast(u32)")

            constructor_match = re.match(r"^(\w+)\((.*)\)$", optional_default)
            if constructor_match is not None:
                constructor_name, args = constructor_match.groups()
                if constructor_name == "sizeof":
                    optional_default = "size_of(" + args + ")"
                else:
                    optional_default = constructor_name + ".{" + args + "}"

            enum_name = get_enum_name(structs_and_enums["enums"], arg_type, optional_default)
            if enum_name is not None:
                optional_default = f".{enum_name}"

            optional_default = convert_enum_default(structs_and_enums["enums"], optional_default)

            if jai_arg_type == "*u8":
                # jai as of beta 0.0.024 has a bug where string
                # default arguments to #foreign procs don't work,
                # so we'll wrap those functions.
                needs_defaults_wrapper = True
                wrapper_arg_type = "string";
                call_arg_value = name + ".data"
                if optional_default == "null":
                    optional_default = '""'

            default_str = f" = {optional_default}"

        arg_infos.append(ArgInfo(name, jai_arg_type, default_str, wrapper_arg_type, call_arg_value))

    return arg_infos, needs_defaults_wrapper


def main():
    # get symbols from windows dll
    if not os.path.isfile(PATH_TO_IMGUI_DLL):
        print("error - expected file to exist: " + PATH_TO_IMGUI_DLL, file=sys.stderr)
        sys.exit(1)

    symbols = get_windows_symbols(PATH_TO_IMGUI_DLL)
    if len(symbols) == 0:
        sys.exit(1)

    # parse out their demangled descriptions and group them by function name
    symbols_grouped = group_symbols(symbols)

    # parse the structs/enums JSON
    structs_and_enums = load_structs_and_enums()

    ctx["output_file"] = open(OUTPUT_BINDINGS_JAI_FILE, "w")
    size_tester_file = open("imgui_sizes.cpp", "w")

    def p_sizer(*a, **k):
        k['file'] = size_tester_file
        return p(*a, **k)

    p_sizer("""\
#include <stdio.h>
#define IMGUI_API __declspec(dllimport)
#include "imgui.h"
#include "imgui_internal.h"

int main(int argc, char** argv) {
    printf("[1]\\n\\n");
""")

    def p_sizer_for_name(name, jai_name):
        p_sizer(f"""    printf("{cimgui_name} {jai_name} %lld\\n", sizeof({cimgui_name}));""")


    #
    # enums
    #
    print_section("ENUMS")
    for cimgui_name, enum_values in structs_and_enums["enums"].items():
        if "Private_" in cimgui_name: continue
        if cimgui_name.endswith("_"):
            cimgui_name = cimgui_name[:-1]

        jai_enum_name = strip_im_prefixes(cimgui_name)
        enum_or_enum_flags = "enum_flags" if "Flags" in jai_enum_name else "enum"

        '''
        highest_value = max(v['calc_value'] for v in enum_values)
        needs_signed = any(v['calc_value'] < 0 for v in enum_values)


        enum_size = ""
        if needs_signed:
            if highest_value < 2 ** 63: enum_size = " s4"
            if highest_value < 2 ** 31: enum_size = " s32"
            if highest_value < 2 ** 15: enum_size = " s16"
            #if highest_value < 2 ** 7: enum_size = " s8"
        else:
            if highest_value < 2 ** 64: enum_size = " 64"
            if highest_value < 2 ** 32: enum_size = " u32"
            if highest_value < 2 ** 16: enum_size = " u16"
            #if highest_value < 2 ** 8: enum_size = " u8"
        '''
        enum_size = " s32"

        p(f"{jai_enum_name} :: {enum_or_enum_flags}{enum_size} {{")

        # p_sizer_for_name(cimgui_name, jai_enum_name);

        output_entries = []
        max_name_len = 0
        for entry_idx, entry in enumerate(enum_values):
            name = entry['name']
            assert name.startswith(cimgui_name), f"{name} does not start with '{cimgui_name}'"
            name = name[len(cimgui_name):]
            if name.startswith("_"):
                name = name[1:]

            max_name_len = max(len(name), max_name_len)

            value = entry['value']
            if isinstance(value, str):
                # omit the type name
                if cimgui_name + "_" in value:
                    value = value.replace(cimgui_name + "_", f"{jai_enum_name}.")
                else:
                    value = value.replace(cimgui_name,       f"{jai_enum_name}.")

            output_entries.append([name, 0, value])
        for i in range(len(output_entries)):
            output_entries[i][1] = max_name_len
        
        for name, num_spaces, value in output_entries:
            space = ' ' * (num_spaces - len(name) + 1)
            p(f"    {name}{space}:: {value};")

        p(f"}}\n")

    #
    # functions
    #

    # parse the definitions JSON
    print_section("FUNCTIONS")
    definitions = json.load(open(generator_output_dir + "definitions.json", "r"))
    func_count = 0

    struct_functions = defaultdict(list)
    global_functions = list()

    for ig_name, overloads in definitions.items():
        if SKIP_INTERNAL and all(e.get('location', None) == "internal" for e in overloads):
            stats["skipped_internal_functions"] += 1
            continue

        for entry in overloads:
            if entry.get("destructor", False):
                # print(f"TODO: destructor {entry['cimguiname']}")
                continue
            if entry.get("constructor", False):
                # print(f"TODO: constructor {entry['cimguiname']}")
                continue
            if entry.get("location", None) == "internal":
                continue

            assert "funcname" in entry, str(entry)

            jai_func_name = entry["funcname"]
            stname = entry.get("stname", None) or None

            args_info, needs_defaults_wrapper = get_jai_args(structs_and_enums, entry)

            ret_type = entry.get("ret", None)
            if ret_type == "void": ret_type = None
            ret_val_with_arrow = f" -> {to_jai_type(ret_type)}" if ret_type is not None else ""

            dll_symbol = get_function_symbol(symbols_grouped, structs_and_enums, entry)
            if dll_symbol is None: continue
            foreign_decl = "#foreign imgui_lib \"" + dll_symbol + "\""

            stats['actual_function_matches'] += 1

            if needs_defaults_wrapper:
                args_string = ", ".join("{name}: {jai_arg_type}{default_str}".format(**k._asdict()) for k in args_info)
                wrapper_args_string = ", ".join("{name}: {wrapper_arg_type}{default_str}".format(**k._asdict()) for k in args_info)

                jai_func_name_internal = "_internal_" + jai_func_name
                call_args = ", ".join("{call_arg_value}".format(**k._asdict()) for k in args_info)
                function_definition = f""" :: ({wrapper_args_string}){ret_val_with_arrow} {{
                {jai_func_name_internal} :: ({args_string}){ret_val_with_arrow} {foreign_decl};
                {"return " if ret_val_with_arrow else ""}{jai_func_name_internal}({call_args});
            }}"""
            else:
                args_string = ", ".join(
                    "{name}: {jai_arg_type}{default_str}".format(**k._asdict())
                    for k in args_info)
                function_definition = f" :: ({args_string}){ret_val_with_arrow} {foreign_decl};"


            jai_function_line = (jai_func_name, function_definition)

            if stname is not None:
                struct_functions[strip_im_prefixes(stname)].append(jai_function_line);
            else:
                global_functions.append(jai_function_line)
                stats["printed_functions"] += 1
            func_count += 1

    p_aligned(global_functions)


    # 
    # structs
    #
    print_section("STRUCTS")

    def include_struct(item):
        if SKIP_INTERNAL:
            cimgui_name, fields = item
            if (structs_and_enums["locations"][cimgui_name] == "internal") and cimgui_name not in allow_internal:
                stats["skipped_internal_structs"] += 1

                if cimgui_name in diagnose_funcnames:
                    print(f"diagnosing {cimgui_name} but about to skip it!")
                    sys.exit(1)

                return False
        
        return True

    all_structs = structs_and_enums["structs"].items()
    struct_items = [item for item in all_structs if include_struct(item)]
    print(f"including {len(struct_items)} structs out of {len(all_structs)}.")

    for struct_idx, (cimgui_name, fields) in enumerate(struct_items):
        jai_struct_name = strip_im_prefixes(cimgui_name)
        if cimgui_name not in skip_structs_for_size:
            p_sizer_for_name(cimgui_name, jai_struct_name)
        p(f"{jai_struct_name} :: struct {{")
        bitfield_state = []
        for field_idx, field in enumerate(fields):
            # TODO: things like ImGuiStoragePair have a field named "" for its union
            if field['name'] == "":
                continue 

            place, jai_name, jai_type = get_jai_field(field, field_idx, fields)
            p(f"    {place}{jai_name}: {jai_type};")
        struct_funcs = struct_functions.get(jai_struct_name, None)
        if struct_funcs is not None:
            p("")
            p_aligned(struct_funcs, prefix=4 * " ")
            stats['printed_struct_functions'] += len(struct_funcs)

        p(f"}}\n")

    p_sizer("}")

    p(extra_code)

    # TODO: take type definitions in extra_code out into a table and re-use
    # them in jai_types_equivalent

    # show stats
    print(f"skipped {len(inline_functions_skipped)} inline functions:", ', '.join(inline_functions_skipped))
    print(f"\nMISSED {len(functions_skipped)} functions:", ', '.join(functions_skipped))
    pprint(dict(stats))

foo = dict(nomatch_verbose = False)

diagnose_funcnames = frozenset([
    # "ListBox",
    # "GetItemRectMin",
] + sys.argv[1:])


def get_function_symbol(symbols_grouped, structs_and_enums, function_entry):
    # Given a list of function symbols from the DLL, attempt to match by
    # function name, argument and return types, and namespace. Returns
    # the mangled symbol name.

    enums = structs_and_enums["enums"]
    funcname = function_entry["funcname"]
    funcs = symbols_grouped[funcname]
    skip_reasons = []

    def nomatch():
        if len(funcs) == 0:
            # an inline function has no actual DLL code. we'll have to figure out how to either make them manually...or...
            inline_functions_skipped.append(funcname)
            return

        stats['total_no_matches'] += 1

        stname = function_entry.get("stname", None)
        functions_skipped.append((f"{stname}::" if stname else "") + funcname)

        if foo['nomatch_verbose'] or funcname in diagnose_funcnames:
            print(f"===============\nno match: " + funcname)
            print("functions in dll: " + pformat(funcs))
            print("function in json: " + pformat(function_entry))
            print("skip reasons:\n" + pformat(skip_reasons))
            if stats['total_no_matches'] > 5:
                print("...stopping showing no matches")
                foo['nomatch_verbose'] = False

    for dll_func in funcs:
        if dll_func['nspace_or_stname'] != function_entry.get("stname", None) and \
            dll_func['nspace_or_stname'] != function_entry.get("namespace", None):
            skip_reasons.append(
                ("nspace/stname", (dll_func['nspace_or_stname'], function_entry.get('stname'), function_entry.get('namespace'))))
            continue

        jai_dll_ret = to_jai_type(dll_func['retval'])
        jai_entry_ret = to_jai_type(function_entry["ret"])
        equiv, reason = jai_types_equivalent(enums, jai_dll_ret, jai_entry_ret)
        if not equiv:
            skip_reasons.append(("return value (json, dll)", (jai_dll_ret, jai_entry_ret)))
            continue

        argsT = function_entry["argsT"]
        if len(dll_func["args"]) != len(argsT):

            if len(dll_func["args"]) + 1 == len(argsT) and \
                argsT[0]["name"] == "self":
                    # skip the self argument generated by the cimgui docs
                    # and consider it a match
                    argsT = argsT[1:]
            else:
                skip_reasons.append("argument counts")
                continue

        arg_types = [to_jai_type(a["type"]) for a in argsT]
        dll_types = [to_jai_type(a) for a in dll_func['args']]
        if funcname in diagnose_funcnames:
            print("dll_types", dll_types)
            print("orig     ", dll_func['args'])
        zipped_types = list(zip(arg_types, dll_types))
        all_equiv, reason, idx = all_jai_types_equivalent(enums, zipped_types)
        if not all_equiv:
            skip_reasons.append(("argument types (json, dll)", (reason, dict(index=idx), zipped_types)))
            continue

        if funcname in diagnose_funcnames:
            print("----")
            print(f"success for '{funcname}': {dll_func['mangled']}")
            print("----")

        return dll_func['mangled']

    nomatch()
    return None

def print_enum_sizes():
    structs_and_enums = load_structs_and_enums()
    #for structs_and_enums["enums"]

            

assert to_jai_type("const char*") == "*u8", "uhoh: " + to_jai_type("const_char*")

def test_get_jai_func_ptr():
    cpp_func = "bool (*)(void*,int,char const* *)"
    expected_jai_func = "(unnamed0: *void, unnamed1: s32, unnamed2: **u8) -> bool #c_call"
    res = get_jai_func_ptr(cpp_func)
    assert res == expected_jai_func,\
        "for: {}\nexpected: {}\nbut got:  {}".format(cpp_func, expected_jai_func, res)


test_get_jai_func_ptr()


if __name__ == "__main__":
    #if len(sys.argv) > 0 and sys.argv[1] == "enumsizes":
        #print_enum_sizes()
    #else:
        main()

