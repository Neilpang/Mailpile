import datetime
import re
import time
import urllib2
from email import encoders
from email.mime.base import MIMEBase

from mailpile.conn_brokers import Master as ConnBroker
from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n
from mailpile.commands import Command
from mailpile.crypto.gpgi import GnuPG
from mailpile.crypto.gpgi import OpenPGPMimeSigningWrapper
from mailpile.crypto.gpgi import OpenPGPMimeEncryptingWrapper
from mailpile.crypto.gpgi import OpenPGPMimeSignEncryptWrapper
from mailpile.crypto.mime import UnwrapMimeCrypto, MessageAsString
from mailpile.crypto.state import EncryptionInfo, SignatureInfo
from mailpile.mailutils import Email, ExtractEmails, ClearParseCache
from mailpile.mailutils import MakeContentID
from mailpile.plugins import PluginManager, EmailTransform
from mailpile.plugins.vcard_gnupg import PGPKeysImportAsVCards
from mailpile.plugins.search import Search

_plugins = PluginManager(builtin=__file__)


##[ GnuPG e-mail processing ]#################################################

class ContentTxf(EmailTransform):
    def TransformOutgoing(self, sender, rcpts, msg, **kwargs):
        matched = False
        gnupg = None
        sender_keyid = None

        # Prefer to just get everything from the profile VCard, in the
        # common case...
        profile = self.config.vcards.get_vcard(sender)
        if profile:
            sender_keyid = profile.pgp_key
            crypto_format = profile.crypto_format

        # Parse the openpgp_header data from the crypto_format
        openpgp_header = [p.split(':')[-1]
                          for p in crypto_format.split('+')
                          if p.startswith('openpgp_header:')]
        if not openpgp_header:
            openpgp_header = self.config.prefs.openpgp_header and ['CFG']

        if openpgp_header[0] != 'N' and not sender_keyid:
            # This is a fallback: this shouldn't happen much in normal use
            try:
                gnupg = gnupg or GnuPG(self.config)
                seckeys = dict([(uid["email"], fp) for fp, key
                                in gnupg.list_secret_keys().iteritems()
                                if key["capabilities_map"].get("encrypt")
                                and key["capabilities_map"].get("sign")
                                for uid in key["uids"]])
                sender_keyid = seckeys.get(sender)
            except (KeyError, TypeError, IndexError, ValueError):
                traceback.print_exc()

        if sender_keyid and openpgp_header:
            preference = {
                'ES': 'signencrypt',
                'SE': 'signencrypt',
                'E': 'encrypt',
                'S': 'sign',
                'N': 'unprotected',
                'CFG': self.config.prefs.openpgp_header
            }[openpgp_header[0].upper()]
            msg["OpenPGP"] = ("id=%s; preference=%s"
                              % (sender_keyid, preference))

        if ('attach-pgp-pubkey' in msg and
                msg['attach-pgp-pubkey'][:3].lower() in ('yes', 'tru')):
            # FIXME: Check attach_pgp_pubkey for instructions on which key(s)
            #        to attach. Attaching all of them may be a bit lame.
            gnupg = gnupg or GnuPG(self.config)
            keys = gnupg.address_to_keys(ExtractEmails(sender)[0])
            key_count = 0
            for fp, key in keys.iteritems():
                if not any(key["capabilities_map"].values()):
                    continue
                # We should never really hit this more than once. But if we
                # do, should still be fine.
                keyid = key["keyid"]
                data = gnupg.get_pubkey(keyid)

                try:
                    from_name = key["uids"][0]["name"]
                    filename = _('Encryption key for %s.asc') % from_name
                except:
                    filename = _('My encryption key.asc')
                att = MIMEBase('application', 'pgp-keys')
                att.set_payload(data)
                encoders.encode_base64(att)
                del att['MIME-Version']
                att.add_header('Content-Id', MakeContentID())
                att.add_header('Content-Disposition', 'attachment',
                               filename=filename)
                att.signature_info = SignatureInfo(parent=msg.signature_info)
                att.encryption_info = EncryptionInfo(parent=msg.encryption_info)
                msg.attach(att)
                key_count += 1

            if key_count > 0:
                msg['x-mp-internal-pubkeys-attached'] = "Yes"

        return sender, rcpts, msg, matched, True

class CryptoTxf(EmailTransform):
    def TransformOutgoing(self, sender, rcpts, msg,
                          crypto_policy='none',
                          prefer_inline=False,
                          cleaner=lambda m: m,
                          **kwargs):
        matched = False
        if 'pgp' in crypto_policy or 'gpg' in crypto_policy:
            wrapper = None
            if 'sign' in crypto_policy and 'encrypt' in crypto_policy:
                wrapper = OpenPGPMimeSignEncryptWrapper
            elif 'sign' in crypto_policy:
                wrapper = OpenPGPMimeSigningWrapper
            elif 'encrypt' in crypto_policy:
                wrapper = OpenPGPMimeEncryptingWrapper
            if wrapper:
                msg = wrapper(self.config,
                              sender=sender,
                              cleaner=cleaner,
                              recipients=rcpts
                              ).wrap(msg, prefer_inline=prefer_inline)
                matched = True

        return sender, rcpts, msg, matched, (not matched)


_plugins.register_outgoing_email_content_transform('500_gnupg', ContentTxf)
_plugins.register_outgoing_email_crypto_transform('500_gnupg', CryptoTxf)

##[ Misc. GPG-related API commands ]##########################################

class GPGKeySearch(Command):
    """Search for a GPG Key."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/searchkey', 'crypto/gpg/searchkey', '<terms>')
    HTTP_CALLABLE = ('GET', )
    HTTP_QUERY_VARS = {'q': 'search terms'}

    class CommandResult(Command.CommandResult):
        def as_text(self):
            if self.result:
                return '\n'.join(["%s: %s <%s>" % (keyid, x["name"], x["email"]) for keyid, det in self.result.iteritems() for x in det["uids"]])
            else:
                return _("No results")

    def command(self):
        args = list(self.args)
        for q in self.data.get('q', []):
            args.extend(q.split())

        return self._gnupg().search_key(" ".join(args))


class GPGKeyReceive(Command):
    """Fetch a GPG Key."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/receivekey', 'crypto/gpg/receivekey', '<keyid>')
    HTTP_CALLABLE = ('POST', )
    HTTP_QUERY_VARS = {'keyid': 'ID of key to fetch'}

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        keyid = self.data.get("keyid", self.args)
        res = []
        for key in keyid:
            res.append(self._gnupg().recv_key(key))

        # Previous crypto evaluations may now be out of date, so we
        # clear the cache so users can see results right away.
        ClearParseCache(pgpmime=True)

        return res


class GPGKeyImport(Command):
    """Import a GPG Key."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/importkey', 'crypto/gpg/importkey',
                '<key_file>')
    HTTP_CALLABLE = ('POST', )
    HTTP_QUERY_VARS = {
        'key_data': 'ASCII armor of public key to be imported',
        'key_file': 'Location of file containing the public key',
        'key_url': 'URL of file containing the public key',
        'name': '(ignored)'
    }

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        key_files = self.data.get("key_file", []) + [a for a in self.args
                                                     if not '://' in a]
        key_urls = self.data.get("key_url", []) + [a for a in self.args
                                                   if '://' in a]
        key_data = []
        key_data.extend(self.data.get("key_data", []))
        for key_file in key_files:
            with open(key_file) as file:
                key_data.append(file.read())
        for key_url in key_urls:
            with ConnBroker.context(need=[ConnBroker.OUTGOING_HTTP]):
                uo = urllib2.urlopen(key_url)
            key_data.append(uo.read())

        rv = self._gnupg().import_keys('\n'.join(key_data))

        # Previous crypto evaluations may now be out of date, so we
        # clear the cache so users can see results right away.
        ClearParseCache(pgpmime=True)

        # Update the VCards!
        PGPKeysImportAsVCards(self.session,
                              arg=([i['fingerprint'] for i in rv['updated']] +
                                   [i['fingerprint'] for i in rv['imported']])
                              ).run()

        return self._success(_("Imported %d keys") % len(key_data), rv)


class GPGKeySign(Command):
    """Sign a key."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/signkey', 'crypto/gpg/signkey', '<keyid> [<signingkey>]')
    HTTP_CALLABLE = ('POST',)
    HTTP_QUERY_VARS = {'keyid': 'The key to sign',
                       'signingkey': 'The key to sign with'}

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        signingkey = None
        keyid = None
        args = list(self.args)
        try: keyid = args.pop(0)
        except: keyid = self.data.get("keyid", None)
        try: signingkey = args.pop(0)
        except: signingkey = self.data.get("signingkey", None)

        print keyid
        if not keyid:
            return self._error("You must supply a keyid", None)
        rv = self._gnupg().sign_key(keyid, signingkey)

        # Previous crypto evaluations may now be out of date, so we
        # clear the cache so users can see results right away.
        ClearParseCache(pgpmime=True)

        return rv


class GPGKeyImportFromMail(Search):
    """Import a GPG Key."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/importkeyfrommail',
                'crypto/gpg/importkeyfrommail', '<mid>')
    HTTP_CALLABLE = ('POST', )
    HTTP_QUERY_VARS = {'mid': 'Message ID', 'att': 'Attachment ID'}
    COMMAND_CACHE_TTL = 0

    class CommandResult(Command.CommandResult):
        def __init__(self, *args, **kwargs):
            Command.CommandResult.__init__(self, *args, **kwargs)

        def as_text(self):
            if self.result:
                return "Imported %d keys (%d updated, %d unchanged) from the mail" % (
                    self.result["results"]["count"],
                    self.result["results"]["imported"],
                    self.result["results"]["unchanged"])
            return ""

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        session, config, idx = self.session, self.session.config, self._idx()
        args = list(self.args)
        if args and args[-1][0] == "#":
            attid = args.pop()
        else:
            attid = self.data.get("att", 'application/pgp-keys')
        args.extend(["=%s" % x for x in self.data.get("mid", [])])
        eids = self._choose_messages(args)
        if len(eids) < 0:
            return self._error("No messages selected", None)
        elif len(eids) > 1:
            return self._error("One message at a time, please", None)

        email = Email(idx, list(eids)[0])
        fn, attr = email.extract_attachment(session, attid, mode='inline')
        if attr and attr["data"]:
            res = self._gnupg().import_keys(attr["data"])

            # Previous crypto evaluations may now be out of date, so we
            # clear the cache so users can see results right away.
            ClearParseCache(pgpmime=True)

            return self._success("Imported key", res)

        return self._error("No results found", None)


class GPGKeyList(Command):
    """List GPG Keys."""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/keylist',
                'crypto/gpg/keylist', '<address>')
    HTTP_CALLABLE = ('GET', )
    HTTP_QUERY_VARS = {'address': 'E-mail address'}

    def command(self):
        args = list(self.args)
        if len(args) > 0:
            addr = args[0]
        else:
            addr = self.data.get("address", None)

        if addr is None:
            return self._error("Must supply e-mail address", None)

        res = self._gnupg().address_to_keys(addr)
        return self._success("Searched for keys for e-mail address", res)


class GPGKeyListSecret(Command):
    """List Secret GPG Keys"""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/keylist/secret',
                'crypto/gpg/keylist/secret', '<address>')
    HTTP_CALLABLE = ('GET', )

    def command(self):
        res = self._gnupg().list_secret_keys()
        return self._success("Searched for secret keys", res)


class GPGUsageStatistics(Search):
    """Get usage statistics from mail, given an address"""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/statistics',
                'crypto/gpg/statistics', '<address>')
    HTTP_CALLABLE = ('GET', )
    HTTP_QUERY_VARS = {'address': 'E-mail address'}
    COMMAND_CACHE_TTL = 0

    class CommandResult(Command.CommandResult):
        def __init__(self, *args, **kwargs):
            Command.CommandResult.__init__(self, *args, **kwargs)

        def as_text(self):
            if self.result:
                return "%d%% of e-mail from %s has PGP signatures (%d/%d)" % (
                    100*self.result["ratio"],
                    self.result["address"],
                    self.result["pgpsigned"],
                    self.result["messages"])
            return ""

    def command(self):
        args = list(self.args)
        if len(args) > 0:
            addr = args[0]
        else:
            addr = self.data.get("address", None)

        if addr is None:
            return self._error("Must supply an address", None)

        session, idx = self._do_search(search=["from:%s" % addr])
        total = 0
        for messageid in session.results:
            total += 1

        session, idx = self._do_search(search=["from:%s" % addr,  "has:pgp"])
        pgp = 0
        for messageid in session.results:
            pgp += 1

        if total > 0:
            ratio = float(pgp)/total
        else:
            ratio = 0

        res = {"messages": total,
               "pgpsigned": pgp,
               "ratio": ratio,
               "address": addr}

        return self._success("Got statistics for address", res)


class GPGCheckKeys(Search):
    """Sanity check your keys and profiles"""
    ORDER = ('', 0)
    SYNOPSIS = (None, 'crypto/gpg/check_keys', 'crypto/gpg/check_keys',
                '[--all-keys]')
    HTTP_CALLABLE = ('GET', )
    COMMAND_CACHE_TTL = 0

    MIN_KEYSIZE = 2048

    class CommandResult(Command.CommandResult):
        def __init__(self, *args, **kwargs):
            Command.CommandResult.__init__(self, *args, **kwargs)

        def as_text(self):
            if not isinstance(self.result, (dict,)):
                return ''
            if self.result.get('details'):
                message = '%s.\n - %s' % (self.message, '\n - '.join(
                    p['description'] for p in self.result['details']
                ))
            else:
                message = '%s. %s' % (self.message, _('Looks good!'))
            if self.result.get('fixes'):
                message += '\n\n%s\n - %s' % (_('Proposed fixes:'),
                                            '\n - '.join(
                    '\n    * '.join(f) for f in self.result['fixes']
                ))
            return message

    def _fix_gen_key(self, min_bits=2048):
        return [
            _("You need a new key!"),
            _("Run: %s") % '`gpg --gen-key`',
            _("Answer the tool\'s questions: use RSA and RSA, %d bits or more"
              ) % min_bits]

    def _fix_mp_config(self, good_key=None):
        fprint = (good_key['fingerprint'] if good_key else '<FINGERPRINT>')
        return [
           _('Update the Mailpile config to use a good key:'),
           _('IMPORTANT: This MUST be done before disabling the key!'),
           _('Run: %s') % ('`set prefs.gpg_recipient = %s`' % fprint),
           _('Run: %s') % ('`optimize`'),
           _('This key\'s passphrase will be used to log in to Mailpile')]

    def _fix_revoke_key(self, fprint, comment=''):
        return [
            _('Revoke bad keys:') + ('  ' + comment if comment else ''),
            _('Run: %s') % ('`gpg --gen-revoke %s`' % fprint),
            _('Say yes to the first question, follow the instructions'),
            _('A revocation certificate will be shown on screen'),
            _('Copy & paste that, save, and send to people who have the old key'),
            _('You can search for %s to find such people'
              ) % '`is:encrypted to:me`']

    def _fix_disable_key(self, fprint, comment=''):
        return [
            _('Disable bad keys:') + ('  ' + comment if comment else ''),
            _('Run: %s') % ('`gpg --edit-key %s`' % fprint),
            _('Type %s') % '`disable`',
            _('Type %s') % '`save`']

    def command(self):
        session, config = self.session, self.session.config
        args = list(self.args)

        all_keys = '--all-keys' in args
        quiet = '--quiet' in args

        date = datetime.date.today()
        today = date.strftime("%Y-%m-%d")
        date += datetime.timedelta(days=14)
        fortnight = date.strftime("%Y-%m-%d")

        serious = 0
        details = []
        fixes = []
        bad_keys = {}
        good_key = None
        good_keys = {}
        secret_keys = self._gnupg().list_secret_keys()

        for fprint, info in secret_keys.iteritems():
            k_info = {
                'description': None,
                'key': fprint,
                'keysize': int(info.get('keysize', 0)),
            }
            is_serious = True
            exp = info.get('expiration_date')
            if info["disabled"]:
                k_info['description'] = _('%s: --- Disabled.') % fprint
                is_serious = False
            elif (not info['capabilities_map'].get('encrypt') or
                    not info['capabilities_map'].get('sign')):
                if info.get("revoked"):
                    k_info['description'] = _('%s: --- Revoked.'
                                              ) % fprint
                    is_serious = False
                elif exp and exp <= today:
                    k_info['description'] = _('%s: Bad: expired on %s'
                                              ) % (fprint,
                                                   info['expiration_date'])
                else:
                    k_info['description'] = _('%s: Bad: key is useless'
                                              ) % fprint
            elif exp and exp <= fortnight:
                k_info['description'] = _('%s: Bad: expires on %s'
                                          ) % (fprint, info['expiration_date'])
            elif k_info['keysize'] < self.MIN_KEYSIZE:
                k_info['description'] = _('%s: Bad: too small (%d bits)'
                                          ) % (fprint, k_info['keysize'])
            else:
                good_keys[fprint] = info
                if (not good_key
                        or int(good_key['keysize']) < k_info['keysize']):
                    good_key = info
                k_info['description'] = _('%s: OK: %d bits, looks good!'
                                          ) % (fprint, k_info['keysize'])
                is_serious = False

            if k_info['description'] is not None:
                details.append(k_info)
            if is_serious:
                fixes += [self._fix_revoke_key(fprint, _('(optional)')),
                          self._fix_disable_key(fprint)]
                serious += 1
            if fprint not in good_keys:
                bad_keys[fprint] = info

        bad_recipient = False
        if config.prefs.gpg_recipient:
            for k in bad_keys:
                if k.endswith(config.prefs.gpg_recipient):
                    details.append({
                        'gpg_recipient': True,
                        'description': _('%s: Mailpile config uses bad key'
                                         ) % k,
                        'key': k
                    })
                    bad_recipient = True
                    serious += 1

        if bad_recipient and good_key:
            fixes[:0] = [self._fix_mp_config(good_key)]

        profiles = config.vcards.find_vcards([], kinds=['profile'])
        for vc in profiles:
            p_info = {
                'profile': vc.get('x-mailpile-rid').value,
                'email': vc.email,
                'fn': vc.fn
            }
            try:
                if all_keys:
                    vcls = [k.value for k in vc.get_all('key') if k.value]
                else:
                    vcls = [vc.get('key').value]
            except (IndexError, AttributeError):
                vcls = []
            for key in vcls:
                fprint = key.split(',')[-1]
                if fprint and fprint in bad_keys:
                    p_info['key'] = fprint
                    p_info['description'] = _('%(key)s: Bad key in profile'
                                              ' %(fn)s <%(email)s>'
                                              ' (%(profile)s)') % p_info
                    details.append(p_info)
                    serious += 1
            if not vcls:
                p_info['description'] = _('No key for %(fn)s <%(email)s>'
                                          ' (%(profile)s)') % p_info
                details.append(p_info)
                serious += 1

        if len(good_keys) == 0:
            fixes[:0] = [self._fix_gen_key(min_bits=self.MIN_KEYSIZE),
                         self._fix_mp_config()]

        if quiet and not serious:
            return self._success('OK')

        ret = self._error if serious else self._success
        return ret(_('Sanity checked: %d keys in GPG keyring, %d profiles')
                     % (len(secret_keys), len(profiles)),
                   result={'passed': not serious,
                           'details': details,
                           'fixes': fixes})


_plugins.register_commands(GPGKeySearch)
_plugins.register_commands(GPGKeyReceive)
_plugins.register_commands(GPGKeyImport)
_plugins.register_commands(GPGKeyImportFromMail)
_plugins.register_commands(GPGKeySign)
_plugins.register_commands(GPGKeyList)
_plugins.register_commands(GPGUsageStatistics)
_plugins.register_commands(GPGKeyListSecret)
_plugins.register_commands(GPGCheckKeys)
