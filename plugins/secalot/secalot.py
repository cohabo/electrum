from binascii import hexlify
from struct import pack, unpack
import hashlib


from electrum import bitcoin
from electrum.bitcoin import TYPE_ADDRESS, int_to_hex, var_int
from electrum.i18n import _
from electrum.keystore import Hardware_KeyStore, parse_xpubkey
from ..hw_wallet import HW_PluginBase
from electrum.transaction import Transaction
from electrum.util import bfh, bh2u

from electrum_gui.qt import *

from PyQt5.QtWidgets import QDialog

try:
    import hid
    from btchip.btchipComm import HIDDongleHIDAPI
    from btchip.btchip import btchip
    from btchip.btchipUtils import compress_public_key,format_transaction, get_regular_input_script, get_p2sh_input_script
    from btchip.bitcoinTransaction import bitcoinTransaction
    from btchip.btchipException import BTChipException
    LIBRARIES_AVAILABLE = True
except ImportError:
    LIBRARIES_AVAILABLE = False

class Secalot_Client():
    def __init__(self, hidDevice, hidDevicePath):
        self.dongleObject = btchip(hidDevice)
        self.hidDevicePath = hidDevicePath

    def is_pairable(self):
        return True

    def close(self):
        self.dongleObject.dongle.close()

    def timeout(self, cutoff):
        pass

    def is_initialized(self):
        walletInitialized = False

        firmwareVersion = self.dongleObject.getFirmwareVersion()

        if firmwareVersion['specialVersion'] & 0x01 != 0:
            walletInitialized = True

        return walletInitialized

    def label(self):
        return "Secalot"

    def i4b(self, x):
        return pack('>I', x)

    def get_xpub(self, bip32_path, xtype):
        if self.is_initialized() == False:
            return None

        self.check_pin()

        splitPath = bip32_path.split('/')
        if splitPath[0] == 'm':
            splitPath = splitPath[1:]
            bip32_path = bip32_path[2:]
        fingerprint = 0
        if len(splitPath) > 1:
            prevPath = "/".join(splitPath[0:len(splitPath) - 1])
            nodeData = self.dongleObject.getWalletPublicKey(prevPath)
            publicKey = compress_public_key(nodeData['publicKey'])#
            h = hashlib.new('ripemd160')
            h.update(hashlib.sha256(publicKey).digest())
            fingerprint = unpack(">I", h.digest()[0:4])[0]
        nodeData = self.dongleObject.getWalletPublicKey(bip32_path)
        publicKey = compress_public_key(nodeData['publicKey'])
        depth = len(splitPath)
        lastChild = splitPath[len(splitPath) - 1].split('\'')
        childnum = int(lastChild[0]) if len(lastChild) == 1 else 0x80000000 | int(lastChild[0])
        xpub = bitcoin.serialize_xpub(xtype, nodeData['chainCode'], publicKey, depth, self.i4b(fingerprint), self.i4b(childnum))
        return xpub

    def check_pin(self):
            pinVerified = False
            walletInitialized = False

            firmwareVersion = self.dongleObject.getFirmwareVersion()

            if firmwareVersion['specialVersion'] & 0x01 != 0:
                walletInitialized = True
            if firmwareVersion['specialVersion'] & 0x02 != 0:
                pinVerified = True

            if walletInitialized == False:
               raise BaseException("Your Secalot device is wiped.")

            if pinVerified == False:
                remaining_attempts = self.dongleObject.getVerifyPinRemainingAttempts()
                if remaining_attempts != 1:
                    msg = "Enter your PIN - remaining attempts : " + str(remaining_attempts)
                else:
                    msg = "Enter your PIN - WARNING : LAST ATTEMPT. If the PIN is not correct, the dongle will be wiped."
                confirmed, p, pin = self.password_dialog(msg)
                if not confirmed:
                    raise Exception('Aborted by user.')
                pin = pin.encode()
                try:
                    self.dongleObject.verifyPin(pin)
                except BTChipException as e:
                    if e.sw == 0x6982:
                        raise BaseException("Invalid PIN.")
                    elif e.sw == 0x6700:
                        raise BaseException("Invalid PIN length.")
                    elif e.sw == 0x6983:
                        raise BaseException("PIN blocked.")
                    else:
                        raise e

    def password_dialog(self, msg=None):
        response = self.handler.get_word(msg)
        if response is None:
            return False, None, None
        return True, response, response


class Secalot_KeyStore(Hardware_KeyStore):
    hw_type = 'secalot'
    device = 'Secalot'

    def __init__(self, d):
        Hardware_KeyStore.__init__(self, d)
        # Errors and other user interaction is done through the wallet's
        # handler.  The handler is per-window and preserved across
        # device reconnects
        self.force_watching_only = False

    def get_derivation(self):
        return self.derivation        

    def get_client(self):
        return self.plugin.get_client(self)

    def decrypt_message(self, pubkey, message, password):
        raise RuntimeError(_('Encryption and decryption are currently not supported for %s') % self.device)

    def sign_message(self, sequence, message, password):
        client = self.get_client()
        dongle = client.dongleObject
        client.check_pin()
        address_path = self.get_derivation()[2:] + "/%d/%d"%sequence

        if len(message) == 0:
            raise BaseException("Can not sign a zero length message")

        try:
            dongle.signMessagePrepare(address_path, bytearray(message, 'utf8'))
            self.handler.show_message("Comfirm message signing on your device...")
            signature = dongle.signMessageSign()
        except BTChipException as e:
            if e.sw == 0x6985:
                raise BaseException("Operation timed out. Please retry.")
            if e.sw == 0x6a80:
                raise BaseException("Unfortunately, this message cannot be signed. Only alphanumerical messages shorter than 140 characters are supported. Please remove any extra characters (tab, carriage return) and retry.")
            else:
                raise e
        finally:
            self.handler.finished()

        # Parse the ASN.1 signature

        rLength = signature[3]
        r = signature[4 : 4 + rLength]
        sLength = signature[4 + rLength + 1]
        s = signature[4 + rLength + 2:]
        if rLength == 33:
            r = r[1:]
        if sLength == 33:
            s = s[1:]

        # And convert it
        return (bytes)([27 + 4 + (signature[0] & 0x01)]) + r + s

    def sign_transaction(self, tx, password):
        if tx.is_complete():
            return
        inputs = []
        inputsPaths = []
        pubKeys = []
        chipInputs = []
        redeemScripts = []
        signatures = []
        p2shTransaction = False
        segwitTransaction = False

        client = self.get_client()
        dongle = client.dongleObject
        client.check_pin()

        # Fetch inputs of the transaction to sign
        derivations = self.get_tx_derivations(tx)
        for txin in tx.inputs():
            if txin['type'] == 'coinbase':
                raise BaseException("Coinbase not supported")     # should never happen

            if txin['type'] in ['p2sh']:
                p2shTransaction = True

            if txin['type'] in ['p2wpkh-p2sh', 'p2wsh-p2sh']:
                segwitTransaction = True

            if txin['type'] in ['p2wpkh', 'p2wsh']:
                segwitTransaction = True

            pubkeys, x_pubkeys = tx.get_sorted_pubkeys(txin)
            for i, x_pubkey in enumerate(x_pubkeys):
                if x_pubkey in derivations:
                    signingPos = i
                    s = derivations.get(x_pubkey)
                    hwAddress = "%s/%d/%d" % (self.get_derivation()[2:], s[0], s[1])
                    break
            else:
                raise BaseException("No matching x_key for sign_transaction") # should never happen

            redeemScript = Transaction.get_preimage_script(txin)
            inputs.append([txin['prev_tx'].raw, txin['prevout_n'], redeemScript, txin['prevout_hash'], signingPos, txin.get('sequence', 0xffffffff - 1) ])
            inputsPaths.append(hwAddress)
            pubKeys.append(pubkeys)

        # Sanity check
        if p2shTransaction:
            for txin in tx.inputs():
                if txin['type'] != 'p2sh':
                    raise BaseException("P2SH / regular input mixed in same transaction not supported") # should never happen

        txOutput = var_int(len(tx.outputs()))
        for txout in tx.outputs():
            output_type, addr, amount = txout
            txOutput += int_to_hex(amount, 8)
            script = tx.pay_script(output_type, addr)
            txOutput += var_int(len(script)//2)
            txOutput += script
        txOutput = bfh(txOutput)

        self.handler.show_message(_("Confirm transaction on your device..."))

        try:
            # Get trusted inputs from the original transactions
            for utxo in inputs:
                sequence = int_to_hex(utxo[5], 4)
                if segwitTransaction:
                    txtmp = bitcoinTransaction(bfh(utxo[0]))
                    tmp = bfh(utxo[3])[::-1]
                    tmp += bfh(int_to_hex(utxo[1], 4))
                    tmp += txtmp.outputs[utxo[1]].amount
                    chipInputs.append({'value' : tmp, 'witness' : True, 'sequence' : sequence})
                    redeemScripts.append(bfh(utxo[2]))
                elif not p2shTransaction:
                    txtmp = bitcoinTransaction(bfh(utxo[0]))
                    trustedInput = dongle.getTrustedInput(txtmp, utxo[1])
                    trustedInput['sequence'] = sequence
                    chipInputs.append(trustedInput)
                    redeemScripts.append(txtmp.outputs[utxo[1]].script)
                else:
                    txtmp = bitcoinTransaction(bfh(utxo[0]))
                    trustedInput = dongle.getTrustedInput(txtmp, utxo[1])
                    trustedInput['sequence'] = sequence
                    chipInputs.append(trustedInput)
                    redeemScripts.append(bfh(utxo[2]))

            # Sign all inputs
            firstTransaction = True
            inputIndex = 0

            if segwitTransaction:
                dongle.startUntrustedTransaction(True, inputIndex,
                                                            chipInputs, redeemScripts[inputIndex])
                dongle.finalizeInputFull(txOutput)

                while inputIndex < len(inputs):
                    singleInput = [ chipInputs[inputIndex] ]
                    dongle.startUntrustedTransaction(False, 0,
                                                            singleInput, redeemScripts[inputIndex])
                    inputSignature = dongle.untrustedHashSign(inputsPaths[inputIndex], lockTime=tx.locktime)
                    inputSignature[0] = 0x30 # force for 1.4.9+
                    signatures.append(inputSignature)
                    inputIndex = inputIndex + 1
            else:
                while inputIndex < len(inputs):
                    dongle.startUntrustedTransaction(firstTransaction, inputIndex,
                                                            chipInputs, redeemScripts[inputIndex])
                    dongle.finalizeInputFull(txOutput)
                    inputSignature = dongle.untrustedHashSign(inputsPaths[inputIndex], '', lockTime=tx.locktime)
                    inputSignature[0] = 0x30 # force for 1.4.9+
                    signatures.append(inputSignature)
                    inputIndex = inputIndex + 1
        finally:
            pass
            self.handler.finished()

        for i, txin in enumerate(tx.inputs()):
            signingPos = inputs[i][4]
            txin['signatures'][signingPos] = bh2u(signatures[i])
        tx.raw = tx.serialize()


class SecalotPlugin(HW_PluginBase):
    libraries_available = LIBRARIES_AVAILABLE
    keystore_class = Secalot_KeyStore

    DEVICE_IDS = [ 
                   (0x1209, 0x7000, 0x03, 0xff00),
                 ]

    def __init__(self, parent, config, name):
        HW_PluginBase.__init__(self, parent, config, name)
        if self.libraries_available:
            self.device_manager().register_devices(self.DEVICE_IDS)

    def get_secalot_device(self, path):
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(True)
        return HIDDongleHIDAPI(dev, True, False)

    def create_client(self, device, handler):
        self.handler = handler

        client = self.get_secalot_device(device.path)
        if client != None:
            client = Secalot_Client(client, device.path)
        return client

    def setup_device(self, device_info, wizard):        
        devmgr = self.device_manager()
        device_id = device_info.device.id_
        client = devmgr.client_by_id(device_id)
        client.handler = self.create_handler(wizard)

        if client.is_initialized() == False:
            persoResult = client.handler.perso_dialog(client.dongleObject)

            if persoResult == QDialog.Rejected:
                raise BaseException("Canceled by user")

    def get_xpub(self, device_id, derivation, xtype, wizard):
        devmgr = self.device_manager()
        client = devmgr.client_by_id(device_id)
        client.handler = self.create_handler(wizard)
        xpub = client.get_xpub(derivation, xtype)
        return xpub

    def get_client(self, keystore, force_pair=True):
        devmgr = self.device_manager()
        handler = keystore.handler
        with devmgr.hid_lock:
            client = devmgr.client_for_keystore(self, handler, keystore, force_pair)        
        if client != None:
            man_string = client.dongleObject.dongle.device.get_manufacturer_string()
            if man_string == None:
                client.dongleObject = btchip(self.get_secalot_device(client.hidDevicePath))

        return client
