try:
    from jinja2.ext import Extension
    from jinja2.nodes import Dict, Const, Pair, Output, TemplateData, MarkSafe
except:
    raise Exception('jinja2 must exist to use this templating extension')

import types
import logging

from ..util import content_type_helper
from ..service.jammer import Jammer as jam_service
from ..service.speed import Speed as speed_service

__all__ = ['PasteJinjaExtension']

log = logging.getLogger('paste')


class PasteJinjaExtension(Extension):
    tags = frozenset(['paste', 'paste_require', 'paste_dump', 'paste_jam'])
    local_cache = {}
    explode_dependencies = False

    JAM_TAG_ATTR_DEFAULTS = {
        'media': None,
        'async': True,
        'inline': False,
        'conditional': None,
        'charset': None,
    }

    @classmethod
    def create_url(cls, path, **kwargs):
        return path

    def parse(self, parser):
        token = next(parser.stream)

        if token.value == 'paste_dump':
            content_type = parser.stream.expect('name').value
            return Output([
                                           MarkSafe(self.call_method('_render_paste_dump_queue', args=(
                                               Const(content_type),
                                           ), lineno=token.lineno))
                                       ], lineno=token.lineno)
        elif token.value == "paste_jam":
            args = self._extract_stream_args(parser)
            content_types = (k for k in args.keys()
                             if content_type_helper.filename_to_content_type('.' + k))
            node_output = []
            agnostic_arglist = self._parse_jam_args(args)
            agnostic_raw_arglist = self._parse_jam_args(args, output_raw=True)
            for ctype in content_types:
                ctype_arglist = self._parse_jam_args(args, content_type=ctype)
                ctype_raw_arglist = self._parse_jam_args(args, content_type=ctype, output_raw=True)

                if args.get(ctype + ':preserve', args.get('preserve', False)):
                    node_output.append(
                        TemplateData(''.join(
                            self._create_paste_jam(
                                self._get_paste_jammed_modules(ctype),
                                args.get(ctype),
                                ctype,
                                **self._coalesce_ast_jam_args(
                                    dict(
                                        agnostic_raw_arglist,
                                        **ctype_raw_arglist
                                    ),
                                    content_type=ctype
                                )
                            )
                        ))
                    )
                else:
                    node_output.append(self.call_method("_append_to_paste_dump_queue", args=(
                        Dict([
                            Pair(
                                Const('content_type'),
                                Const(ctype)
                            ),
                            Pair(
                                Const('dependencies'),
                                Const(args.get(ctype))
                            ),
                            Pair(
                                Const('args'),
                                Dict(
                                    agnostic_arglist + ctype_arglist
                                )
                            )
                        ]),
                    ), lineno=token.lineno))
                    node_output.append(Const(''))

            return Output(node_output, lineno=token.lineno)

        # NOTE: the following is deprecated
        elif token.value == 'paste':
            # {% paste %} tag that brings in all configured modules
            log.warning('{% paste %} is deprecated. upgrade to: {% paste_dump <content_type> %}')
            content_type = 'js'
            return Output([
                                           MarkSafe(self.call_method('_render_paste_dump_queue', args=(
                                               Const(content_type),
                                           ), lineno=token.lineno))
                                       ], lineno=token.lineno)

        elif token.value == 'paste_require':
            log.warning(
                '{% paste_require %} is deprecated. upgrade to: {% paste_jam <content_type>="<dependencies>" %}')
            module_names = parser.parse_expression().value
            args = self._extract_stream_args(parser)
            content_type = 'js'
            agnostic_arglist = self._parse_jam_args(args)
            agnostic_raw_arglist = self._parse_jam_args(args, output_raw=True)
            ctype_arglist = self._parse_jam_args(args, content_type=content_type)
            ctype_raw_arglist = self._parse_jam_args(args, content_type=content_type, output_raw=True)

            if args.get('preserve', False):
                return Output(
                    [
                        TemplateData(''.join(
                            self._create_paste_jam(
                                self._get_paste_jammed_modules(content_type),
                                module_names,
                                content_type,
                                **self._coalesce_ast_jam_args(
                                    dict(
                                        agnostic_raw_arglist,
                                        **ctype_raw_arglist
                                    ),
                                    content_type
                                )
                            )
                        ))
                    ],
                    lineno=token.lineno)
            else:
                return Output(
                    [
                        self.call_method(
                            "_append_to_paste_dump_queue",
                            args=(
                                Dict([
                                    Pair(
                                        Const('content_type'),
                                        Const(content_type)
                                    ),
                                    Pair(
                                        Const('dependencies'),
                                        Const(module_names)),
                                    Pair(
                                        Const('args'),
                                        Dict(
                                            agnostic_arglist + ctype_arglist
                                        ))
                                ]),
                            ),
                            lineno=token.lineno
                        ),
                        Const('')
                    ],
                    lineno=token.lineno
                )

    @classmethod
    def _get_paste_jammed_modules(cls, content_type):
        try:
            module_names = cls.local_cache.paste_jammed_modules
        except AttributeError:
            module_names = cls.local_cache.paste_jammed_modules = {}
        return module_names.setdefault(content_type, set()) if content_type else set()

    @classmethod
    def _get_paste_dump_queue(cls):
        try:
            q = cls.local_cache.paste_dump_queue
        except AttributeError:
            q = cls.local_cache.paste_dump_queue = {}
        return q

    @classmethod
    def _create_paste_jam(cls, loaded_ctype_modules, paste_modules, content_type, **kwargs):
        contents = ''
        tag_generator = None

        if content_type == 'js':
            tag_generator = cls._create_paste_jam_script_tag

            # special javascript logic to bootsrap the paste core
            if kwargs.get('conditional') is None and 'paste' not in loaded_ctype_modules:
                # don't update the modules if there is a conditional. this is a hack
                # since IE is the only browser that supports conditionals
                jam = jam_service.jam_filter_loaded(content_type, 'paste', loaded_ctype_modules)
                contents += tag_generator(jam=jam, inline=True)
        elif content_type == 'css':
            tag_generator = cls._create_paste_jam_style_tag

        jam = jam_service.jam_filter_loaded(
            content_type,
            paste_modules,
            loaded_ctype_modules.copy() if kwargs.get('conditional') else loaded_ctype_modules
        )

        if not cls.explode_dependencies:
            contents += tag_generator(jam, **kwargs)
        else:
            # to support inlining easily, we'll just make an extra jam instance.
            # this is only applicable for pretty render, so the performance hit is acceptable
            contents += ''.join([tag_generator(
                jam_service(
                    request_path=uri,
                    content_type=content_type
                ), **kwargs
            ) for uri in jam.unjammed_uris])

        return contents

    @classmethod
    def _create_paste_jam_style_tag(cls, jam, media=None, inline=False, conditional=None, charset=None, **kwargs):
        if not isinstance(jam, jam_service) or not jam.uri:
            return ""

        if conditional:
            condwrap = '<!--[%s]%%s<![endif]-->' % conditional
        else:
            condwrap = '%s'

        charset_attr = (' charset="' + str(charset) + '"') if charset else ''
        media_attr = (' media="' + str(media) + '"') if media else ''

        if (((isinstance(inline, types.StringTypes) and
                  (inline.lower() == 'true' or inline.lower() == 'inline')) or (isinstance(inline, bool) and inline)) or
                (speed_service.skip_network(jam.byte_size) and not cls.explode_dependencies)):
            # inline the resource if it is less than 1024 bytes,
            # it's not worth making a network request
            return condwrap % ('<style type="text/css"%s%s>%s</style>' % (charset_attr, media_attr, jam.contents))

        return condwrap % ('<link rel="stylesheet"%s%s href="%s"/>' % (
            charset_attr,
            media_attr,
            cls.create_url(jam.uri)
        ))

    @classmethod
    def _create_paste_jam_script_tag(cls, jam, async=True, inline=False, conditional=None, charset=None, **kwargs):
        if not isinstance(jam, jam_service) or not jam.uri:
            return ""

        if conditional:
            condwrap = '<!--[%s]%%s<![endif]-->' % conditional
        else:
            condwrap = '%s'

        charset_attr = (' charset="' + str(charset) + '"') if charset else ''

        if (((isinstance(inline, types.StringTypes) and
                  (inline.lower() == 'true' or inline.lower() == 'inline')) or (isinstance(inline, bool) and inline)) or
                (speed_service.skip_network(jam.byte_size) and not cls.explode_dependencies)):
            # inline the worth if it is less than 1024 bytes,
            # it's not worth making a network request
            return condwrap % ('<script type="text/javascript"%s>%s</script>' % (charset_attr, jam.contents))

        if ((isinstance(async, types.StringTypes) and (async.lower() == 'true' or async.lower() == 'async')) or
                (isinstance(async, bool) and async)):
            return condwrap % (
                '''<script type="text/javascript"%s>
                (function(d,r){%s!0;r('%s');})(paste.define,paste.require);
                </script>'''.replace('\n            ', '') % (
                    charset_attr,
                    ''.join("d('%s').isLoading=" % d for d in jam.dependencies if d != 'paste'),
                    cls.create_url(jam.uri)
                ))

        return condwrap % ('<script type="text/javascript"%s src="%s"></script>' % (
            charset_attr,
            cls.create_url(jam.uri)
        ))

    @classmethod
    def _extract_stream_args(cls, parser):
        args = {}
        while parser.stream.current.type != 'block_end':
            parser.stream.next_if('comma')
            exp = parser.parse_expression()
            if isinstance(exp, Const):
                continue
            key = exp.name
            colon = parser.stream.next_if('colon')
            if colon:
                key = key + colon.value + parser.parse_expression().name
            assign = parser.stream.next_if('assign')
            if assign:
                value = parser.parse_expression().value
                args[key] = value

        return args

    @classmethod
    def _parse_jam_args(cls, args, content_type=None, output_raw=False):
        ct_wrap = content_type + ':%s' if content_type else '%s'

        if output_raw:
            return dict(((ct_wrap % attr), args.get(ct_wrap % attr)) for attr in cls.JAM_TAG_ATTR_DEFAULTS.keys() if
                        ct_wrap % attr in args)
        else:
            return [
                Pair(
                    Const(ct_wrap % attr),
                    TemplateData(args.get(ct_wrap % attr))
                ) for attr in cls.JAM_TAG_ATTR_DEFAULTS.keys() if ct_wrap % attr in args
            ]

    @classmethod
    def _coalesce_ast_jam_args(cls, args, content_type=None):
        content_type_prefix = content_type + ':'
        coalesced_args = cls.JAM_TAG_ATTR_DEFAULTS.copy()
        coalesced_args.update(dict(
            (k, v) for k, v in args.iteritems()
            if k in cls.JAM_TAG_ATTR_DEFAULTS.keys()
        ))

        if content_type:
            for k, v in args.iteritems():
                if k.startswith(content_type_prefix):
                    key = k[len(content_type_prefix):]
                    if key in cls.JAM_TAG_ATTR_DEFAULTS.keys():
                        coalesced_args[key] = v

        return coalesced_args

    @classmethod
    def _append_to_paste_dump_queue(cls, item):
        q = cls._get_paste_dump_queue()
        ctype_q = q.setdefault(item.get('content_type'), [])
        ctype_q.append(item)
        return ''

    @classmethod
    def _render_paste_dump_queue(cls, content_type):
        jam_list = cls._get_paste_dump_queue().get(content_type, [])
        output = ''.join(cls._create_paste_jam(
            cls._get_paste_jammed_modules(content_type),
            jam.get('dependencies'),
            content_type,
            **cls._coalesce_ast_jam_args(jam.get('args'), content_type=content_type)
        ) for jam in jam_list)

        return output