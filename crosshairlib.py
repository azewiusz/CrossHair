import ast
import collections
import copy
import doctest
import functools
import inspect
import operator
import os
import sys
import z3

import crosshair
from asthelpers import *
import prooforacle

'''

Figure out how to apply rules to standard operators (like we do for builtins)
Determine what is and is not representable in the system
Deal with *args and **kwargs
Normalize: assignments, if statements, and returns
Deal with dependent type references as below.
Understand standalone assertions and rewriting.
Assertions and rewriting references must resolve to a specific method.
Understand user guided proof methods.
Handle variable renaming somehow

def longer(a:isseq, b:isseq) -> ((_ == a) or (_ == b)) and len(_) == max(len(a),len(b))

Lambda dropping:
(1) create a registry (enum) of all lambdas
(2) detecting which lambdas to handle is now about the constraints over the enum

Varying objectives:
(1) Correctness validation; typing
(2) Inlining/partial evaluation/Super optimization
(3) Compilation to LLVM / others
(4) integer range optimization
(5) datastructure selection
(6) JSON parse/serialize API optimization
(7) Memory management optimization

'''

class PureScopeTracker(ScopeTracker):
    def resolve(self, node):
        nodetype = type(node)
        if nodetype is ast.Name:
            refname = node.id
            # print('PureScopeTracker', refname, 'scopes', self.scopes)
            if refname[0] != '_':
                if hasattr(builtins, refname):
                    return _pure_defs.get_fn('_builtin_' + refname).get_definition()
                if hasattr(crosshair, refname):
                    return _pure_defs.get_fn(refname).get_definition()
            elif refname.startswith('_z_'):
                if refname[3].isupper():
                    raise Exception('Invalid Z3 intrinsic: "'+refname+'"')
                zname = refname[3].upper() + refname[4:]
                return getattr(Z, zname)
            return super().resolve(node)
        return node

_SYMCTR = 0
def gensym(name='sym'):
    global _SYMCTR
    ' TODO: How do we ensure symbols cannot possibly conflict? '
    _SYMCTR += 1
    return '{}#{:03d}'.format(name, _SYMCTR)


class PatternVarPreprocessor(ast.NodeTransformer):
    def __init__(self):
        self.mapping = {}
    def visit_Name(self, node):
        nodeid = node.id
        if not nodeid.isupper():
            return node
        else:
            mapping = self.mapping
            astvar = mapping.get(nodeid)
            if astvar is None:
                astvar = '$' + nodeid
                mapping[nodeid] = astvar
            return astcopy(node, id = astvar)

def preprocess_pattern_vars(*nodes):
    processor = PatternVarPreprocessor()
    if len(nodes) == 1:
        return processor.visit(nodes[0])
    else:
        return [processor.visit(node) for node in nodes]

class PatternVarReplacer(ast.NodeTransformer):
    def __init__(self, mapping):
        self.mapping = mapping
    def visit_Name(self, node):
        nodeid = node.id
        if nodeid[0] == '$':
            return copy.deepcopy(self.mapping[nodeid[1:]])
        else:
            return node

def replace_pattern_vars(node, bindings):
    node = copy.deepcopy(node)
    return PatternVarReplacer(bindings).visit(node)

_MATCHER = {}
def matches(node, patt, bind):
    # print(('matched? node=', ast.dump(node)))
    # print(('matched? patt=', ast.dump(patt)))
    typ = type(patt)
    if typ is ast.Name:
        if patt.id[0] == '$':
            bind[patt.id[1:]] = node
            return True
    if typ is not type(node):
        bind.clear()
        return False
    cb = _MATCHER.get(typ)
    if cb:
        return cb(node, patt, bind)
    else:
        raise Exception('Unhandled node type: '+str(typ))
        bind.clear()
        return False

_MATCHER[ast.Call] = lambda n, p, b : (
    matches(n.func, p.func, b) and len(n.args) == len(p.args) and
    all(matches(ni, pi, b) for (ni, pi) in zip(n.args, p.args))
)
_MATCHER[ast.Name] = lambda n, p, b : (
    n.id == p.id
)
_MATCHER[ast.Module] = lambda n, p, b : (
    matches(n.body, p.body, b)
)
_MATCHER[ast.Expr] = lambda n, p, b : (
    matches(n.value, p.value, b)
)
_MATCHER[ast.BinOp] = lambda n, p, b : (
    type(n.op) is type(p.op) and matches(n.left, p.left, b) and matches(n.right, p.right, b)
)
_MATCHER[ast.BoolOp] = lambda n, p, b : (
    type(n.op) is type(p.op) and all(matches(ni, pi, b) for (ni, pi) in zip(n.values, p.values))
)
_MATCHER[ast.Num] = lambda n, p, b : n.n == p.n
_MATCHER[list] = lambda n, p, b: (
    all(matches(ni, pi, b) for (ni, pi) in zip(n,p))
)

_MATCHER[ast.arg] = lambda n, p, b: (
    n.arg == p.arg and n.annotation == p.annotation
)

def _test():
    '''
    >>> bindings = {}
    >>> patt, repl = preprocess_pattern_vars(astparse('0 + X'), astparse('X + 1'))
    >>> unparse(patt)
    '(0 + $X)'
    >>> matches(astparse('0 + 2'), patt, bindings)
    True
    >>> unparse(bindings['X'])
    '2'
    >>> unparse(replace_pattern_vars(repl, bindings))
    '(2 + 1)'
    '''
    pass

def ast_in(item, lst):
    print(unparse(lst), ast.In)
    return ast.Compare(left=item, ops=[ast.In()], comparators=[lst])

_AST_SUB_HASH = {
    ast.BinOp : lambda n: type(n.op),
    ast.Call : lambda n: n.func if isinstance(n.func, str) else 0,
    ast.BoolOp : lambda n: type(n.op)
}
def patthash(node):
    nodetype = type(node)
    return (hash(nodetype) << 8) + hash(_AST_SUB_HASH.get(nodetype, lambda n: 0)(node))

class Replacer(ast.NodeTransformer):
    '''
    Simple tranformer that just uses a lambda.
    '''
    def __init__(self, logic):
        self.logic = logic
    def __call__(self, node):
        return self.visit(node)
    def generic_visit(self, node):
        ret = self.logic(node)
        if (ret is node):
            return super().generic_visit(node)
        else:
            return ret

def patt_to_lambda(patt, argvars):
    if ( # check whether we can skip the lambda...
        type(patt) is ast.Call and
        type(patt.func) is ast.Name and
        len(patt.args) == len(argvars) and
        all(type(a) is ast.Name and
            a.id[0] == '$' and
            a.id[1:].text == v for (a,v) in zip(patt.args, argvars))
    ):
        return patt.func
    varmap = [(v,gensym(v)) for v in argvars]
    return ast.Lambda(
        args=ast.arguments(args=[ast.arg(arg=v2,annotation=None) for _,v2 in varmap],defaults=[],vararg='',kwarg=''),
        body=replace_pattern_vars(patt, {v1:ast.Name(id=v2) for v1,v2 in varmap})
    )

def beta_reduce(node):
    '''
    >>> unparse(beta_reduce(exprparse('(lambda x:x+1)(5)')))
    '(5 + 1)'
    '''
    if type(node) is not ast.Call:
        return node
    func = node.func
    if type(func) is ast.Name:
        return node
    if type(func) is not ast.Lambda:
        raise Exception()
    ret = inline(node, func)
    # print('beta reduce', unparse(node), unparse(ret))
    return ret

class AdvancedRewriter(PureScopeTracker):
    def __init__(self):
        super().__init__()
    def __call__(self, root):
        self.root = root
        self.result = None
        self.visit(root)
        return self.result if self.result else self.root

    def visit_Call(self, node):
        newnode = beta_reduce(node)
        if newnode is not node:
            return self.visit(newnode)
        node = newnode
        callfn = self.resolve(node.func)
        # print('in expr', ast.dump(node))
        # print('function getting called', ast.dump(callfn) if callfn else '')
        if callfn and getattr(callfn,'name',None) == 'reduce' and node is not self.root:
            reducefn = self.resolve(node.args[0])
            print('reduce callback',ast.dump(node.args[0]))
            if isinstance(reducefn, (ast.Lambda, ast.FunctionDef)):
                self.attempt_reduce_fn_xform(node, reducefn, node.args[1], node.args[2])
        return super().generic_visit(node)

    def attempt_reduce_fn_xform(self, callnode, reducefn, inputlist, initializer):
        ancestors = Replacer(lambda n: ast.Name(id='$R') if n is callnode else n)(self.root)
        # print('ancestors', unparse(ancestors))
        argnames = {a.arg for a in reducefn.args.args}
        inverse = lambda n: ast.Call(func=ast.Name(id='inverse*'),args=[n],keywords=[])
        body_with_inverses = Replacer(
            lambda n: inverse(n) if (type(n) is ast.Name and n.id in argnames) else n
        )(fn_expr(reducefn))
        # print('body_with_inverses', unparse(body_with_inverses))
        body_to_simplify = replace_pattern_vars(ancestors, {'R': body_with_inverses})
        # print('body_to_simplify', unparse(body_to_simplify))
        inverse_canceller = WrappedRewriteEngine(basic_simplifier)
        inverse_canceller.add(
            replace_pattern_vars(ancestors, {'R': inverse(ast.Name(id='I'))}),
            ast.Name(id='I'),
            always
        )
        simplified = inverse_canceller.rewrite(body_to_simplify)
        if matches(simplified, ast.Name(id='inverse*'), {}):
            return
        print('success reduce-fn transform:', unparse(simplified))
        # transformation OK
        reducefn.body = simplified
        new_inputlist = ast.Call(func=ast.Name(id='map'), args=[patt_to_lambda(ancestors, ['R']), inputlist], keywords={})
        new_initializer = replace_pattern_vars(ancestors, {'R': initializer})
        new_reduce = ast.Call(func=ast.Name(id='reduce'), args=[reducefn, new_inputlist, new_initializer], keywords={})

        self.result = new_reduce

class RewriteEngine(ast.NodeTransformer):
    def __init__(self):
        self._index = collections.defaultdict(list)
    def lookup(self, hsh):
        return self._index[hsh]
    def add(self, patt, repl, cond):
        patt, repl = preprocess_pattern_vars(patt, repl)
        self.lookup(patthash(patt)).append( (patt, repl, cond) )
    def generic_visit(self, node):
        while True:
            node = super().generic_visit(node)
            newnode = self.rewrite_top(node)
            if newnode is node:
                return node
            node = newnode
    def rewrite_top(self, node):
        while True:
            bind = {}
            matched = False
            for candidate in self.lookup(patthash(node)):
                patt, repl, cond = candidate
                if matches(node, patt, bind):
                    if not cond(bind):
                        continue
                    matched = True
                    newnode = replace_pattern_vars(repl, bind)
                    print('rewrite found ', unparse(patt))
                    print('rewrite', unparse(node), ' => ', unparse(newnode))
                    node = newnode
                    break
            if not matched:
                break
        return node
    def rewrite(self, node):
        return self.visit(node)

class WrappedRewriteEngine(RewriteEngine):
    def __init__(self, inner):
        self.inner = inner
        super().__init__()
    def lookup(self, hsh):
        r1 = self._index[hsh]
        r2 = self.inner.lookup(hsh)
        if not r1:
            return r2
        if not r2:
            return r1
        return r1 + r2

def normalize_binop(bindings):
    f = bindings['F']
    if type(f) is ast.Lambda:
        varnames = {a.arg for a in f.args.args}
        if type(f.body) is ast.BoolOp:
            boolop = f.body
            if (
                len(boolop.values) == 2 and
                all(type(v) is ast.Name for v in boolop.values) and
                varnames == {v.id for v in boolop.values}
            ):
                optype = type(boolop.op)
                if optype is ast.Or:
                    bindings['F'] = f = ast.Name(id='or*')
                elif optype is ast.And:
                    bindings['F'] = f = ast.Name(id='and*')
                else:
                    raise Exception()
                return True
    return False


basic_simplifier = RewriteEngine()
always = lambda x:True
# TODO: rewriting needs to resolve references -
# you cannot just rewrite anything named "isfunc"
# Or can you? Because pure must be imported as * and names cannot be reassigned?
for (patt, repl, condition) in [
    #('ToBool(isbool(X))', 'IsBool(X)', always),
    #('ToBool(isint(X))', 'IsInt(X)', always),
    #('ToBool(X and Y)', 'And(ToBool(X), ToBool(Y))', always),
    #('ToBool(isint(X))', 'IsInt(X)', always),
    # ('IsTruthy(X and Y)', 'And(IsTruthy(X), IsTruthy(Y))', always),
    #('IsTruthy(isint(X))', 'IsInt(X)', always),
    #('IsInt(X + Y)', 'And(IsInt(X), IsInt(Y))', always),
    #('IsBool(isbool(X))', 'Z3True', always),
    # ('IsBool(isnat(X))', 'Z3True', always), # do not need other types
    # ('IsBool(islist(X))', 'Z3True', always),
    # ('IsBool(isfunc(X))', 'Z3True', always),
    # ('isfunc(WrapFunc(X))', 'Z3True', always),
    # ('isbool(WrapBool(X))', 'Z3True', always),
    #('isint(X)', 'Z3True', lambda b:type(b['X']) is ast.Num),

    # ('isbool(isint(X))', 'True', always),
    # ('isnat(X + Y)', 'isnat(X) and isnat(Y)', always),
    # ('all(map(isnat, range(X)))', 'True', always),
    # ('isnat(X)', 'True', lambda b:type(b['X']) is ast.Num),
    # ('reduce(F,L,I)', 'reduce(F,L,I)', normalize_binop),
    # ('reduce(F,L,I)', 'all(L)', reduce_f_and_i('and*', [True])),
    # ('reduce(F,L,I)', 'any(L)', reduce_f_and_i('or*', [False, None])),
    # ('reduce(F,L,I)', 'False', reduce_f_and_i('and*', [False, None])),
    # ('reduce(F,L,I)', 'True', reduce_f_and_i('or*', [True])),
    # ('isbool(all(X))', 'all(map(isbool, X))', always),
    # ('map(F, map(G, L))', 'map(lambda x:F(G(x)), L)', always), # TODO integrate gensym() in here for lambda argument
    # ('all(map(F, L))', 'True', f_is_always_true),
    # # ('all(map(F,filter(G,L)))', 'all(map(GIMPLIESF,L))', mapfilter),
    # # if F(R(x,y)) rewrites to (some) R`(F(x), F(y))
    # # ('F(reduce(R, L, I))', 'reduce(R`, map(F, L), I)', reduce_fn_check),
]:
    basic_simplifier.add(exprparse(patt), exprparse(repl), condition)

# expr = exprparse('IsTruthy(isint(c) and isint(d))')
# rewritten = basic_simplifier.rewrite(expr)
# print('rewrite engine test', unparse(expr), unparse(rewritten))



PyFunc = z3.DeclareSort('PyFunc')
Unk = z3.Datatype('Unk')
Unk.declare('none')
Unk.declare('bool', ('tobool', z3.BoolSort()))
Unk.declare('int', ('toint', z3.IntSort()))
Unk.declare('func', ('tofunc', PyFunc))
Unk.declare('a', ('tl', Unk), ('hd', Unk))
Unk.declare('_') # empty tuple
Unk.declare('undef') # error value
(Unk,) = z3.CreateDatatypes(Unk)
App = z3.Function('.', Unk, Unk, Unk)


class ZHolder(): pass
Z = ZHolder()
Z.Wrapbool = Unk.bool # z3.Function('Wrapbool', z3.BoolSort(), Unk)
Z.Wrapint = Unk.int # z3.Function('Wrapint', z3.IntSort(), Unk)
Z.Wrapfunc = Unk.func # z3.Function('Wrapfunc', PyFunc, Unk)
Z.Bool = Unk.tobool # z3.Function('Bool', Unk, z3.BoolSort())
Z.Int = Unk.toint # z3.Function('Int', Unk, z3.IntSort())
Z.Func = Unk.tofunc # z3.Function('Func', Unk, PyFunc)
Z.Isbool = lambda x:Unk.is_bool(x)
Z.Isint = lambda x:Unk.is_int(x)
Z.Isfunc = lambda x:Unk.is_func(x)
Z.Istuple = lambda x:z3.Or(Unk.is_a(x), Unk.is__(x))
Z.Isnone = lambda x:Unk.is_none(x)
Z.Isdefined = lambda x:z3.Not(Unk.is_undef(x))
Z.Eq = lambda x,y: x == y
Z.Neq = lambda x,y: x != y
Z.Distinct = z3.Distinct
Z.T = z3.Function('T', Unk, z3.BoolSort())
Z.F = z3.Function('F', Unk, z3.BoolSort())
Z.N = Unk.none # z3.Const('None', Unk)
Z.Implies = z3.Implies
Z.And = z3.And
Z.Or = z3.Or
Z.Not = z3.Not
Z.Lt = lambda x,y: x < y
Z.Lte = lambda x,y: x <= y
Z.Gt = lambda x,y: x > y
Z.Gte = lambda x,y: x >= y
Z.Add = lambda x,y: x + y
Z.Sub = lambda x,y: x - y
Z.Concat = z3.Function('Concat', Unk, Unk, Unk)
# forall and exists are syntactically required to contain a lambda with one argument
Z.Forall = z3.ForAll
Z.Thereexists = z3.Exists

_z3_name_constants = {
    True: Z.Wrapbool(True),
    False: Z.Wrapbool(False),
    None: Z.N,
}

_fndef_to_moduleinfo = {}
def get_scope_for_def(fndef):
    return _fndef_to_moduleinfo[fndef].get_scope_for_def(fndef)

class ModuleInfo:
    def __init__(self, module, module_ast):
        self.module = module
        self.ast = module_ast
        self.functions = {'': FnInfo('', self)} # this is for global assertions, which have no corresponding definitional assertion
    def fn(self, name):
        if name not in self.functions:
            self.functions[name] = FnInfo(name, self)
        return self.functions[name]
    def get_fn(self, name):
        return self.functions[name]
    def get_scope_for_def(self, fndef):
        class FnFinder(PureScopeTracker):
            def __init__(self):
                super().__init__()
                self.hit = None
            def visit_FunctionDef(self, node):
                if node is fndef:
                    self.hit = [copy.deepcopy(s) for s in self.scopes]
                return node
        f = FnFinder()
        f.visit(self.ast)
        return f.hit

def astand(clauses):
    if len(clauses) == 1:
        return clauses[0]
    else:
        return ast.BoolOp(op=ast.And, values=clauses)

def fn_annotation_assertion(fn):
    args = fn_args(fn)
    preconditions = argument_preconditions(args)
    if not preconditions and not fn.returns:
        return None
    predicate = fn.returns if fn.returns else ast.Name(id='isdefined')
    varnamerefs = [ast.Name(id=a.arg) for a in args]
    expectation = astcall(predicate, astcall(ast.Name(id=fn.name), *varnamerefs))
    fdef = ast.FunctionDef(
        name = '_assertdef_'+fn.name,
        args=fn.args,
        body=[ast.Return(value=expectation)],
        decorator_list=[],
        returns=None
    )
    # print('expectation')
    # print(unparse(fn))
    # print(unparse(fdef))
    return fdef

class FnInfo:
    def __init__(self, name, moduleinfo):
        self.name = name
        self.moduleinfo = moduleinfo
        self.assertions = []
        self.definition = None
        self.definitional_assertion = None
    def add_assertion(self, assertion):
        self.assertions.append(assertion)
        _fndef_to_moduleinfo[assertion] = self.moduleinfo
    def set_definition(self, definition):
        if self.definition is not None:
            raise Exception('multiply defined function: '+str(self.name))
        self.definition = definition
        _fndef_to_moduleinfo[definition] = self.moduleinfo
        definitional_assertion = fn_annotation_assertion(definition)
        if definitional_assertion:
            self.definitional_assertion = definitional_assertion

    def get_definition(self):
        return self.definition
    def get_assertions(self):
        return self.assertions

def parse_pure(module):
    module_ast = ast.parse(open(module.__file__).read())
    ret = ModuleInfo(module, module_ast)
    for item in module_ast.body:
        itemtype = type(item)
        if itemtype == ast.FunctionDef:
            name = item.name
            if name.startswith('_assert_'):
                name = name[len('_assert_'):]
                ret.get_fn(name).add_assertion(item)
            else:
                ret.fn(name).set_definition(item)
    return ret

_pure_defs = parse_pure(crosshair)

def fn_for_op(optype):
    return _pure_defs.get_fn('_op_' + optype).get_definition()

_z3_fn_ids = set(id(x) for x in Z.__dict__.values())

def _merge_arg(accumulator, arg):
    if (type(arg) == ast.Starred):
        if accumulator == Unk._:
            return arg.value
        else:
            return Z.Concat(accumulator, arg.value)
    else:
        return Unk.a(accumulator, arg)

def z3apply(fnval, args):
    if id(fnval) in _z3_fn_ids:
        return fnval(*args)
    else:
        return App(fnval, functools.reduce(_merge_arg, args, Unk._))

class Z3BindingEnv(collections.namedtuple('Z3BindingEnv',['refs','support'])):
    def __new__(cls, refs=None):
        return super(Z3BindingEnv, cls).__new__(cls, refs if refs else {}, [])

class Z3Transformer(PureScopeTracker): #ast.NodeTransformer):
    def __init__(self, env):
        super().__init__()
        self.env = env

    def transform(self, module, fnname):
        pass

    # def visit(self, node):
    #     print('visit', unparse(node))
    #     return super().visit(node)

    def generic_visit(self, node):
        raise Exception('Unhandled ast - to - z3 transformation: '+str(type(node)))

    def register(self, definition):
        # print('register?', definition)
        refs = self.env.refs
        if type(definition) == ast.Name:
            raise Exception('Undefined identifier: "{}" at line {}[:{}]'.format(
                definition.id,
                getattr(definition, 'lineno', ''),
                getattr(definition, 'col_offset', '')))
            return z3.Const(definition.id, Unk)
        if type(definition) == ast.arg:
            name = definition.arg
        elif type(definition) == ast.FunctionDef:
            name = definition.name
        else: # z3 function (z3 functions must be called immediately - they are not values)
            # print('register abort : ', repr(definition))
            return definition
            # if hasattr(definition, 'name'):
            #     name = definition.name()
            # else:
            #     name = definition.__name__
        if definition not in refs:
            # print('register done  : ', str(name))
            # print('register new Unk value', name, definition)
            # print('register.', name)
            refs[definition] = z3.Const(name, Unk)
        return refs[definition]

    def visit_Subscript(self, node):
        return self.handle_subscript(self.visit(node.value), node.slice)

    def handle_subscript(self, value, subscript):
        subscripttype = type(subscript)
        if subscripttype is ast.Index:
            self.env.ops.add(Get)
            return z3apply(fn_for_op('Get'), (value, self.visit(subscript.value)))
        elif subscripttype is ast.Slice:
            if subscript.step is None:
                self.env.ops.add(SubList)
                return z3apply(fn_for_op('SubList'), (value, self.visit(subscript.lower), self.visit(subscript.upper)))
            else:
                self.env.ops.add(SteppedSubList)
                return z3apply(fn_for_op('SteppedSubList'), (value, self.visit(subscript.lower), self.visit(subscript.upper), self.visit(subscript.step)))
        elif subscripttype is ast.ExtSlice:
            return functools.reduce(
                lambda a, b: z3apply(fn_for_op('Add'), (a, b)),
                (self.handle_subscript(value, dim) for dim in index.dims))

    def visit_NameConstant(self, node):
        return _z3_name_constants[node.value]

    def visit_Name(self, node):
        return self.register(self.resolve(node))

    def visit_Starred(self, node):
        # this node will get handled later, in z3apply()
        return ast.Starred(value=self.visit(node.value))

    def visit_BinOp(self, node):
        z3fn = self.register(fn_for_op(type(node.op).__name__))
        left, right = self.visit(node.left), self.visit(node.right)
        return z3apply(z3fn, (left, right))

    def visit_UnaryOp(self, node):
        z3fn = self.register(fn_for_op(type(node.op).__name__))
        val = self.visit(node.operand)
        return z3apply(z3fn, (val,))

    def visit_BoolOp(self, node):
        z3fn = self.register(fn_for_op(type(node.op).__name__))
        args = [self.visit(v) for v in node.values]
        return functools.reduce(lambda a,b:z3apply(z3fn,(a,b)), args)

    def visit_Compare(self, node):
        ret = None
        z3and = lambda : self.register(fn_for_op('And'))
        def add(expr, clause):
            return clause if expr is None else z3apply(z3and(), [clause, expr])
        lastval = self.visit(node.comparators[-1])
        for op, left in reversed(list(zip(node.ops[1:], node.comparators[:-1]))):
            z3fn = self.register(fn_for_op(type(op).__name__))
            left = self.visit(left)
            ret = add(ret, z3apply(z3fn, [left, lastval]))
            lastval = left

        z3fn = self.register(fn_for_op(type(node.ops[0]).__name__))
        ret = add(ret, z3apply(z3fn, [self.visit(node.left), lastval]))
        return ret

    def visit_Num(self, node):
        return Z.Wrapint(node.n)

    def function_body_to_z3(self, func):
        argpairs = [(a.value.arg,True) if type(a)==ast.Starred else (a.arg,False) for a in func.args.args]

        argnames = [a.arg for a in func.args.args]
        argvars = [z3.Const(name, Unk) for name in argnames]
        arg_name_to_var = dict(zip(argnames, argvars))
        arg_name_to_def = {name: ast.arg(arg=name) for name in argnames}
        self.scopes.append(arg_name_to_def)
        for name, definition in arg_name_to_def.items():
            self.env.refs[definition] = arg_name_to_var[name]
        z3body = self.visit(func.body)
        self.scopes.pop()

        z3vars = [
            ast.Starred(value=arg_name_to_var[n]) if is_starred else arg_name_to_var[n]
            for n, is_starred in argpairs
        ]

        return z3body, z3vars

    def visit_Lambda(self, node):
        name = 'lambda_{}_{}'.format(node.lineno, node.col_offset)
        funcval = Z.Wrapfunc(z3.Const(name, PyFunc))

        z3body, z3vars = self.function_body_to_z3(node)
        z3application = z3apply(funcval, z3vars)
        stmt = z3.ForAll(z3vars, z3application == z3body, patterns=[z3application])
        self.env.support.append(stmt)
        self.env.support.append(Unk.is_func(funcval))
        return funcval

    def visit_Tuple(self, node):
        #z3fn = self.register(_pure_defs.get_fn('_builtin_tuple').get_definition())
        if type(node.ctx) != ast.Load:
            raise Exception(ast.dump(node))
        params = [self.visit(a) for a in node.elts]
        # print('visit tuple ', *[p for p in params])
        return functools.reduce(_merge_arg, params, Unk._)
        # return functools.reduce(Unk.a, params, Unk._)
        # return z3apply(z3fn, params)

    def visit_Call(self, node):
        newnode = beta_reduce(node)
        if newnode is not node:
            return self.visit(newnode)
        z3fn = self.visit(node.func)
        # Special case for quantifiers:
        if z3fn is z3.ForAll or z3fn is z3.Exists:
            targetfn = node.args[0]
            if type(targetfn) != ast.Lambda:
                raise Exception('Quantifier argument must be a lambda')
                # z3varargs = z3.Const(gensym('a'), Unk)
                # targetfn = self.visit(targetfn)
                # return z3fn([z3varargs], Z.T(App(targetfn, Unk.a(Unk._, z3varargs))))
            z3body, z3vars = self.function_body_to_z3(targetfn)
            # print(' - ', z3fn, z3vars, z3body)
            return z3fn(z3vars, Z.T(z3body))
            # z3varargs = z3.Const(gensym('a'), Unk)
            # targetfn = self.visit(targetfn)
            # return z3fn([z3varargs], Z.T(App(targetfn, Unk.a(Unk._, z3varargs))))
            # applied = App(targetfn, Unk.a(Unk._, z3varargs))
            # applied = ast.Call(fn=targetfn, args=[Unk.a(Unk._, z3varargs))
            # return z3fn([z3varargs], Z.T(self.visit(beta_reduce(applied)))
            # targetfn = self.visit(targetfn.)

        params = [self.visit(a) for a in node.args]
        # special case forall & thereexists
        return z3apply(z3fn, params)

def to_z3(node, env, initial_scopes=None):
    '''
    >>> to_z3(exprparse('False'), Z3BindingEnv())
    bool(False)
    >>> to_z3(exprparse('range(4)'), Z3BindingEnv())
    .(_builtin_range, a(_, int(4)))
    >>> to_z3(exprparse('(4,*())'), Z3BindingEnv())
    Concat(a(_, int(4)), _)
    >>> to_z3(exprparse('(*range,4)'), Z3BindingEnv())
    a(_builtin_range, int(4))
    >>> to_z3(exprparse('4 + 0'), Z3BindingEnv())
    .(_op_Add, a(a(_, int(4)), int(0)))
    >>> to_z3(exprparse('True and False'), Z3BindingEnv())
    .(_op_And, a(a(_, bool(True)), bool(False)))
    >>> to_z3(exprparse('(lambda x:True)(7)'), Z3BindingEnv())
    bool(True)
    >>> to_z3(exprparse('0 <= 5'), Z3BindingEnv())
    .(_op_LtE, a(a(_, int(0)), int(5)))
    >>> to_z3(exprparse('0 <= 5 < 9'), Z3BindingEnv()) # doctest: +NORMALIZE_WHITESPACE
    .(_op_And, a(a(_,
      .(_op_LtE, a(a(_, int(0)), int(5)))),
      .(_op_Lt,  a(a(_, int(5)), int(9)))))
    '''
    transformer = Z3Transformer(env)
    if initial_scopes:
        transformer.scopes = initial_scopes
    return transformer.visit(node)

def call_predicate(predicate, target):
    expr = ast.Call(func=predicate, args=[target], keywords=[])
    return basic_simplifier.rewrite(expr)

def to_assertion(expr, target, env, extra_args=()):
    call = call_predicate(expr, target)
    return to_z3(call, env)

def solve(assumptions, conclusion, oracle):
    make_repro_case = os.environ.get('CH_REPRO') in ('1','true')
    solver = z3.Solver()
    z3.set_param(
        verbose = 1,
    )
    opts = {
        'timeout': 10000,
        'unsat_core': True,
        'macro-finder': True,
        'smt.mbqi': False,
        'smt.pull-nested-quantifiers': False,
    }
    solver.set(**opts)
    # solver.set('smt.mbqi', True)
    # solver.set('smt.timeout', 1000),
    # solver.set(auto_config=True)
    # solver.set('macro-finder', True)
    # solver.set('smt.pull-nested-quantifiers', True)

    required_assumptions = [a for a in assumptions if a.score is None]
    assumptions = [a for a in assumptions if a.score is not None]
    assumptions.sort(key=lambda a:a.score)
    assumptions = assumptions[:120]
    # for l in assumptions:
        # print('baseline:', l)
    assumptions = required_assumptions + assumptions
    # assumptions = [a.expr for a in assumptions]

    stmt_index = {}
    if make_repro_case:
        for assumption in assumptions:
            solver.add(assumption.expr)
        solver.add(z3.Not(conclusion))
    else:
        for idx, assumption in enumerate(assumptions):
            label = 'assumption{}'.format(idx)
            stmt_index[label] = assumption
            solver.assert_and_track(assumption.expr, label)
        solver.assert_and_track(z3.Not(conclusion), 'conclusion')

    core = set()
    try:
        ret = solver.check()
        if ret == z3.unsat:
            if not make_repro_case:
                print ('BEGIN PROOF CORE ')
                core = set(map(str, solver.unsat_core()))
                for stmt in core:
                    if stmt == 'conclusion': continue
                    ast = stmt_index[stmt].src_ast
                    if ast:
                        print(unparse(ast))
                        #print(' '+stmt_index[stmt].expr.sexpr())
                print ('END PROOF CORE ')
                if 'conclusion' not in core:
                    raise Exception('Soundness failure; conclusion not required for proof')
            ret = True
        elif ret == z3.sat:
            print('Counterexample:')
            print(solver.model())
            ret = False
        else:
            ret = None
    except z3.z3types.Z3Exception as e:
        if e.value != b'canceled':
            raise e
        ret = None
    with open('repro.smt2', 'w') as fh:
        if make_repro_case:
            fh.write(solver.sexpr())
            fh.write("\n(check-sat)\n(get-model)\n")
            print('Wrote repro smt file.')
    report = [(unparse(a.src_ast), k in core) for k, a in stmt_index.items() if a.src_ast]
    return ret, report

def calls_name(expr):
    if type(expr) == ast.Call and type(expr.func) == ast.Name:
        return expr.func.id

def find_weight(expr):
    if type(expr) == ast.FunctionDef:
        for dec in expr.decorator_list:
            if calls_name(dec) == 'ch_weight':
                return dec.args[0].n
    return 1
    # return None

def find_patterns(expr, found_implies=False):
    if type(expr) == ast.FunctionDef:
        declared_patterns = []
        for dec in expr.decorator_list:
            if calls_name(dec) == 'ch_pattern':
                multipattern_contents = []
                for lam in dec.args:
                    if type(lam) != ast.Lambda:
                        raise Exception()
                    if tuple(a.arg for a in lam.args.args) != tuple(a.arg for a in expr.args.args):
                        print('pattern mismatch: ', unparse(lam.args), unparse(expr.args))
                        raise Exception('pattern arguments do not match function arguments')
                    # print('DECL ', unparse(lam.body))
                    multipattern_contents.append(lam.body)
                declared_patterns.append(multipattern_contents)
        return declared_patterns if declared_patterns else find_patterns(fn_expr(expr))
    if type(expr) == ast.Lambda:
        return find_patterns(fn_expr(expr))
    fnname = calls_name(expr)
    if fnname == '_z_wrapbool':
        return find_patterns(expr.args[0], found_implies=found_implies)
    elif fnname == 'implies' or fnname == '_z_implies':
        return find_patterns(expr.args[1], found_implies=True)
    else:
        # check for equals in various forms
        if fnname == '_z_eq':
            return find_patterns(expr.args[0], found_implies=found_implies)
            # return find_patterns(expr.args[1], found_implies=found_implies)
        if type(expr) == ast.Compare:
            if len(expr.ops) == 1 and type(expr.ops[0]) == ast.Eq:
                # return find_patterns(expr.comparators[0], found_implies=found_implies)
                return find_patterns(expr.left, found_implies=found_implies)
    return [[expr]]

_Z3_implies_decl = z3.Implies(False, True).decl()
_Z3_eq_decl = (z3.Int('x') == 0).decl()
def z3_implication_equality_form(expr, found_implies=False):
    if expr.decl() == _Z3_implies_decl:
        return z3_implication_equality_form(expr.arg(1), found_implies=True)
    elif expr.decl() == _Z3_eq_decl:
        return expr.arg(0), expr.arg(1)
    if found_implies:
        return (expr,)
    return None

_NO_VAL = object()
def assertion_fn_to_z3(fn, env, scopes, weight=_NO_VAL):
    args = fn_args(fn)
    expr = fn_expr(fn)
    scopes.append({a.arg:a for a in args})
    z3expr = to_z3(expr, env, scopes)
    # print('  assertion_fn_to_z3 ', getattr(fn,'name'), ' : ', z3expr.sexpr())
    if z3expr.decl() == Z.Wrapbool:
        z3expr = z3expr.arg(0)
    else:
        z3expr = Z.T(z3expr)

    print(unparse(fn))
    forall_kw = {}
    if args:
        multipatterns = find_patterns(fn)
        if weight == 1:
            multipatterns = []
        forall_kw['weight'] = find_weight(fn) if weight is _NO_VAL else weight
        #print(' ', z3expr.sexpr())
        #patt = form_ret[0] if form_ret else expr
        # print(' ' , 'patterns:', [[unparse(p) for p in m] for m in multipatterns])
        if multipatterns:
            # TODO check that the pattern expression covers the bound variables
            # if getattr(fn,'name', None) ==  '_assert_isnat':
            #     patterns.append(to_z3(expr.comparators[0], env, scopes))
            #     print('+++++++',patterns[1].sexpr())
            # print(patterns[0].sexpr())
            # TODO: does this matter? Is it required? map 4 test appears to require ut now
            # probably can accomplish this with ch_pattern
            for pattern_exprs in multipatterns:
                if len(pattern_exprs) == 1:
                    patt = pattern_exprs[0]
                    if calls_name(patt) in ['isbool','isint','isnat','istuple','isfunc','isnone']:
                        isdefexpr = ast.Call(func=ast.Name(id='isdefined'), args=[patt.args[0]], kwargs=[])
                        multipatterns.append([isdefexpr])
            forall_kw['patterns'] = [
                to_z3(m[0], env, scopes) if len(m) == 1 else z3.MultiPattern(*[to_z3(p, env, scopes) for p in m])
                for m in multipatterns
            ]
            print('   ', forall_kw['patterns'])

    preconditions = argument_preconditions(args)
    if preconditions:
        if len(preconditions) == 1:
            z3expr = Z.Implies(Z.T(to_z3(preconditions[0], env, scopes)), z3expr)
        else:
            z3expr = Z.Implies(Z.And([Z.T(to_z3(p, env, scopes)) for p in preconditions]), z3expr)

    z3arg_constants = [env.refs[a] for a in args if a in env.refs]
    if z3arg_constants:
        z3expr = z3.ForAll(z3arg_constants, z3expr, **forall_kw)
    # print('  ', 'forallkw', forall_kw, z3expr.sexpr())
    return z3expr

def make_statement(first, env=None, scopes=None):
    if env is None:
        return Z3Statement(None, first)
    else:
        if find_weight(first) is None:
            return None
        return Z3Statement(first, assertion_fn_to_z3(first, env, scopes))

class Z3Statement:
    def __init__(self, src_ast, expr):
        self.score = None
        self.src_ast = src_ast
        self.expr = expr
    def set_score(self, score):
        self.score = score
    def __str__(self):
        return 'Z3Statement(score={}, src={})'.format(
            self.score, unparse(self.src_ast) if self.src_ast else self.expr.sexpr())

def check_assertion_fn(conclusion_fn, conclusion_ast, oracle=None):
    conclusion_src = inspect.getsource(conclusion_fn).strip()
    # print('c ', repr(conclusion_src))
    counterexample = prooforacle.find_counterexample(conclusion_fn)
    tree = ast.parse(conclusion_src)

    if type(tree) == ast.Module:
        tree = tree.body[0]
    # tree = fn_expr(tree)

    ret, report = prove_assertion_fn(tree, oracle=oracle)

    if counterexample is not None:
        print('Counterexample found: ', counterexample)
        if ret is True:
            raise Exception('Counterexample conflicts with proof')
        return False, []
    else:
        if ret is False:
            print('Cannot prove, but cannot find counterexample')

    return ret, report

def prove_assertion_fn(conclusion_fn, oracle=None):
    env = Z3BindingEnv()

    print('Checking assertion:')
    print(unparse(conclusion_fn))
    print()

    conclusion = assertion_fn_to_z3(conclusion_fn, env, [], weight=1)

    # print('Using support:')
    baseline = []
    baseline.extend(map(make_statement, core_assertions(env)))
    # always-include assertions
    for a in _pure_defs.get_fn('').get_assertions():
        # print(' ', unparse(a))
        baseline.append(make_statement(a, env, []))

    _MAX_DEPTH = 10 # TODO experiment with this
    handled = set()
    for iter in range(_MAX_DEPTH):
        baseline.extend(map(make_statement, env.support[:]))
        env = Z3BindingEnv(env.refs) # clear env.support
        borderset = set(env.refs.keys()) - handled
        addedone = False
        for name, fninfo in _pure_defs.functions.items():
            fn_def = fninfo.get_definition()
            if fn_def in borderset:
                addedone = True
                handled.add(fn_def)
                for assertion in fninfo.get_assertions():
                    # print('.A. ', unparse(assertion))
                    scopes = get_scope_for_def(assertion)
                    baseline.append(make_statement(assertion, env, scopes))
                baseline.append(make_statement(Unk.is_func(env.refs[fn_def]))) # not implied by datatypes, when used as a standalone reference
                if fninfo.definitional_assertion:
                    # print('.D. ', unparse(fninfo.definitional_assertion))
                    scopes = get_scope_for_def(fn_def)
                    baseline.append(make_statement(fninfo.definitional_assertion, env, scopes))
        if not addedone:
            print('Completed knowledge expansion after {} iterations: {} functions.'.format(iter, len(handled)))
            break

    baseline = [s for s in baseline if s is not None]
    if oracle is None:
        oracle = prooforacle.TrivialProofOracle()
    # print ()
    # print ('conclusion:', unparse(conclusion_fn))
    oracle.score_axioms(baseline, conclusion_fn)
    return solve(baseline, conclusion, oracle)

def core_assertions(env):
    refs = env.refs
    isint, isbool, _builtin_len, _builtin_tuple, _op_Add = [
        _pure_defs.get_fn(name).get_definition()
        for name in (
            'isint', 'isbool', '_builtin_len', '_builtin_tuple', '_op_Add')
    ]
    n = z3.Const('n', z3.IntSort())
    i = z3.Const('i', z3.IntSort())
    b = z3.Const('b', z3.BoolSort())
    r = z3.Const('r', Unk)
    g = z3.Const('g', Unk)
    x = z3.Const('x', Unk)
    baseline = []

    # r + () = r
    baseline.append(z3.ForAll([r],
        Z.Eq(Z.Concat(r, Unk._), r),
        patterns=[Z.Concat(r, Unk._)]
    ))

    # () + r = r
    baseline.append(z3.ForAll([r],
        Z.Eq(Z.Concat(Unk._, r), r),
        patterns=[Z.Concat(Unk._, r)]
    ))

    # f(g, *(x,), ...) = f(g, x, ...)
    # TODO think this is derivable fromt he other two
    baseline.append(z3.ForAll([x, g],
        Z.Eq(
            Z.Concat(g, Unk.a(Unk._, x)),
            Unk.a(g, x)
        ),
        patterns=[Z.Concat(g, Unk.a(Unk._, x))]
    ))

    # f(g, *(*r, x), ...) = f(g, *r, x, ...)
    baseline.append(z3.ForAll([x, g, r],
        Z.Eq(
            Z.Concat(g, Unk.a(r, x)),
            Unk.a(Z.Concat(g, r), x)
        ),
        patterns=[Z.Concat(g, Unk.a(r, x))]
    ))

    # if isint in refs:
    #     baseline.append(z3.ForAll([n], Z.T(App(refs[isint], Unk.a(Unk._, Z.Wrapint(n))))))
    # if isbool in refs:
    #     baseline.append(z3.ForAll([b], Z.T(App(refs[isbool], Unk.a(Unk._, Z.Wrapbool(b))))))
    # if _builtin_len in refs and _builtin_tuple in refs:
    #     baseline.append(App(refs[_builtin_len],
    #         Arg(ArgStart, App(refs[_builtin_tuple],
    #             ArgStart
    #         ))
    #     ) == Z.Wrapint(0))
    #     baseline.append(z3.ForAll([a, x],
    #         App(refs[_builtin_len],
    #             Arg(ArgStart, App(refs[_builtin_tuple], Arg(a, x)))
    #         ) ==
    #         App(refs[_op_Add], Arg(Arg(ArgStart,
    #             App(refs[_builtin_len], Arg(ArgStart, App(refs[_builtin_tuple], a)))),
    #             Z.Wrapint(1))
    #         )
    #     ))
    return baseline


# class FunctionChecker(ast.NodeTransformer):
#     def visit_FunctionDef(self, node):
#         node = self.generic_visit(node)
#         print('function def',
#             node.name,
#             [(a.arg,a.annotation) for a in node.args.args],
#             node.returns,
#             node.body)
#         check_assertion_fn(node)
#         return node
#
def pcompile(*functions):
    fn_to_module = {f:inspect.getmodule(f) for f in functions}
    for module in set(fn_to_module.values()):
        # tree = ast.parse(inspect.getsource(module))
        # x = FunctionChecker().visit(tree) # .body[0].body[0].value
        for fn in module.getmembers(inspect.isfunction):
            check_assertion_fn(fn)
            # tree = ast.parse(inspect.getsource(fn))
            # x = FunctionChecker().visit(tree) # TODO overkill

    return functions if len(functions) > 1 else functions[0]



if __name__ == "__main__":
    import doctest
    doctest.testmod()