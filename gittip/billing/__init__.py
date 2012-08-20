"""This module encapsulates billing logic and db access.

There are two pieces of information for each customer related to billing:

    balanced_account_uri    NULL - This customer has never been billed.
                            'deadbeef' - This customer's card has been
                                validated and associated with a Balanced
                                account.
    last_bill_result        NULL - This customer has not been billed yet.
                            '' - This customer is in good standing.
                            <message> - An error message.

"""
from __future__ import unicode_literals

import balanced
import stripe
from aspen.utils import typecheck
from gittip import db


def associate(participant_id, balanced_account_uri, card_uri):
    """Given three unicodes, return a dict.

    This function attempts to associate the credit card details referenced by
    card_uri with a Balanced Account. If the attempt succeeds we cancel the
    transaction. If it fails we log the failure. Even for failure we keep the
    balanced_account_uri, we don't reset it to None/NULL. It's useful for
    loading the previous (bad) credit card info from Balanced in order to
    prepopulate the form.

    """
    typecheck( participant_id, unicode
             , balanced_account_uri, (unicode, None)
             , card_uri, unicode
              )


    # Load or create a Balanced Account.
    # ==================================

    email_address = '{}@gittip.com'.format(participant_id)
    if balanced_account_uri is None:
        # arg - balanced requires an email address
        try:
            customer = \
               balanced.Account.query.filter(email_address=email_address).one()
        except balanced.exc.NoResultFound:
            customer = balanced.Account(email_address=email_address).save()
        CUSTOMER = """\

                UPDATE participants
                   SET balanced_account_uri=%s
                 WHERE id=%s

        """
        db.execute(CUSTOMER, (customer.uri, participant_id))
        customer.meta['participant_id'] = participant_id
        customer.save()  # HTTP call under here
    else:
        customer = balanced.Account.find(balanced_account_uri)


    # Associate the card with the customer.
    # =====================================
    # Handle errors. Return a unicode, a simple error message. If empty it
    # means there was no error. Yay! Store any error message from the
    # Balanced API as a string in last_bill_result. That may be helpful for
    # debugging at some point.

    customer.card_uri = card_uri
    try:
        customer.save()
    except balanced.exc.HTTPError as err:
        last_bill_result = err.message.decode('UTF-8')  # XXX UTF-8?
        typecheck(last_bill_result, unicode)
        out = last_bill_result
    else:
        out = last_bill_result = ''

    STANDING = """\

        UPDATE participants
           SET last_bill_result=%s
         WHERE id=%s

    """
    db.execute(STANDING, (last_bill_result, participant_id))
    return out


def associate_bank_account(participant_id, balanced_account_uri,
                           balanced_destination_uri):
    """

    """
    typecheck( participant_id, unicode
             , balanced_account_uri, (unicode, None)
             , balanced_destination_uri, unicode
              )

    account = balanced.Account.find(balanced_account_uri)
    try:
        account.add_bank_account(balanced_destination_uri)
    except balanced.exc.HTTPError as err:
        last_bill_result = err.message.decode('UTF-8')  # XXX UTF-8?
        typecheck(last_bill_result, unicode)
        out = last_bill_result
    else:
        out = last_bill_result = ''

    STANDING = """\

        UPDATE participants
           SET last_ach_result = %s,
               balanced_account_uri = %s,
               balanced_destination_uri = %s
         WHERE id = %s

    """
    db.execute(STANDING, (last_bill_result,
                          balanced_account_uri,
                          balanced_destination_uri,
                          participant_id))
    return out


def clear_bank_account(participant_id, balanced_account_uri):
    typecheck(participant_id, unicode, balanced_account_uri, unicode)

    # accounts in balanced cannot be deleted at the moment. instead we mark all
    # valid cards as invalid which will restrict against anyone being able to
    # issue charges against them in the future.
    customer = balanced.Account.find(balanced_account_uri)
    for bank_account in customer.bank_accounts:
        if bank_account.is_valid:
            bank_account.is_valid = False
            bank_account.save()

    CLEAR = """\

        UPDATE participants
           SET balanced_destination_uri = NULL
             , last_ach_result = NULL
         WHERE id = %s

    """
    db.execute(CLEAR, (participant_id,))


def clear(participant_id, balanced_account_uri):
    typecheck(participant_id, unicode, balanced_account_uri, unicode)

    # accounts in balanced cannot be deleted at the moment. instead we mark all
    # valid cards as invalid which will restrict against anyone being able to
    # issue charges against them in the future.
    customer = balanced.Account.find(balanced_account_uri)
    for card in customer.cards:
        if card.is_valid:
            card.is_valid = False
            card.save()

    CLEAR = """\

        UPDATE participants
           SET balanced_account_uri=NULL
             , last_bill_result=NULL
         WHERE id=%s

    """
    db.execute(CLEAR, (participant_id,))


def store_error(participant_id, msg):
    typecheck(participant_id, unicode, msg, unicode)
    ERROR = """\

        UPDATE participants
           SET last_bill_result=%s
         WHERE id=%s

    """
    db.execute(ERROR, (msg, participant_id))


def store_ach_error(participant_id, msg):
    typecheck(participant_id, unicode, msg, unicode)
    ERROR = """\

        UPDATE participants
           SET last_ach_result=%s
         WHERE id=%s

    """
    db.execute(ERROR, (msg, participant_id))


# Card
# ====
# While we're migrating data we need to support loading data from both Stripe
# and Balanced.


class StripeCard(object):
    """This is a dict-like wrapper around a Stripe PaymentMethod.
    """

    _customer = None  # underlying stripe.Customer object

    def __init__(self, stripe_customer_id):
        """Given a Stripe customer id, load data from Stripe.
        """
        if stripe_customer_id is not None:
            self._customer = stripe.Customer.retrieve(stripe_customer_id)

    def _get(self, name, default=""):
        """Given a name, return a string.
        """
        out = ""
        if self._customer is not None:
            out = self._customer.get('active_card', {}).get(name, "")
            if out is None:
                out = default
        return out

    def __getitem__(self, name):
        """Given a name, return a string.
        """
        if name == 'id':
            out = self._customer.id if self._customer is not None else None
        elif name == 'last4':
            out = self._get('last4')
            if out:
                out = "************" + out
        elif name == 'expiry':
            month = self._get('expiry_month')
            year = self._get('expiry_year')

            if month and year:
                out = "%d/%d" % (month, year)
            else:
                out = ""
        else:
            name = { 'address_1': 'address_line1'
                   , 'address_2': 'address_line2'
                   , 'state': 'address_state'
                   , 'zip': 'address_zip'
                    }.get(name, name)
            out = self._get(name)
        return out


class BalancedCard(object):
    """This is a dict-like wrapper around a Balanced Account.
    """

    _account = None  # underlying balanced.Account object

    def __init__(self, balanced_account_uri):
        """Given a Balanced account_uri, load data from Balanced.
        """
        if balanced_account_uri is not None:
            self._account = balanced.Account.find(balanced_account_uri)

    def _get_card(self):
        """Return the most recent card on file for this account.
        """
        return self._account.cards[-1]

    def _get(self, name, default=""):
        """Given a name, return a unicode.
        """
        out = ""
        if self._account is not None:
            try:
                card = self._get_card()
                out = getattr(card, name, "")
            except IndexError:  # no cards associated
                pass
            if out is None:
                out = default
        return out

    def __getitem__(self, name):
        """Given a name, return a string.
        """
        if name == 'id':
            out = self._account.uri if self._account is not None else None
        elif name == 'last4':
            out = self._get('last_four')
            if out:
                out = "************" + unicode(out)
        elif name == 'expiry':
            month = self._get('expiration_month')
            year = self._get('expiration_year')

            if month and year:
                out = "%d/%d" % (month, year)
            else:
                out = ""
        elif name == 'address_2':
            out = self._get('meta', {}).get('address_2', '')
        elif name == 'state':
            out = self._get('region')
            if not out:
                # There's a bug in balanced where the region does get persisted
                # but doesn't make it back out. This is a workaround until such
                # time as that's fixed.
                out = self._get('meta', {}).get('region', '')
        else:
            name = { 'address_1': 'street_address'
                   , 'zip': 'postal_code'
                    }.get(name, name)
            out = self._get(name)
        return out


class BalancedBankAccount(object):
    """This is a dict-like wrapper around a Balanced Account.
    """

    _account = None  # underlying balanced.Account object
    _bank_account = None

    def __init__(self, balanced_account_uri, balanced_destination_uri):
        """Given a Balanced account_uri, load data from Balanced.
        """
        if not balanced_account_uri:
            return

        self._account = balanced.Account.find(balanced_account_uri)

        if balanced_destination_uri:
            self._bank_account = balanced.BankAccount.find(
                balanced_destination_uri)

    def __getitem__(self, item):
        mapper = {
            'id': 'uri',
            'account_uri': 'account.uri',
            'bank_name': 'bank_name',
            'last_four': 'last_four',
        }
        if item not in mapper:
            raise IndexError()
        if not self._bank_account:
            return None
        # account.uri will become:
        #     tiem = getattr(self._bank_account, 'account')
        #     tiem = getattr(tiem, 'uri')
        tiem  = self._bank_account
        for vals in mapper[item].split('.'):
            tiem = getattr(tiem, vals)
        return tiem

    @property
    def is_setup(self):
        return self._bank_account is not None