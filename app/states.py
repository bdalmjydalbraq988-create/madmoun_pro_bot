from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    customer_input = State()
    confirmation = State()


class DepositFlow(StatesGroup):
    amount = State()
    reference = State()
    payer_account = State()
    proof = State()


class AdminPriceFlow(StatesGroup):
    value = State()


class AdminProductEditFlow(StatesGroup):
    value = State()


class AdminWalletFlow(StatesGroup):
    user_id = State()
    amount = State()
    reason = State()


class AdminUserLookupFlow(StatesGroup):
    user_id = State()


class AdminProductFlow(StatesGroup):
    category = State()
    name = State()
    description = State()
    price = State()
    fulfillment = State()
    provider_product_id = State()
    input_label = State()


class AdminCategoryFlow(StatesGroup):
    name = State()


class AdminDeliveryFlow(StatesGroup):
    value = State()


class AdminRefundFlow(StatesGroup):
    reason = State()


class AdminPaymentRejectFlow(StatesGroup):
    reason = State()


class AdminChannelRateFlow(StatesGroup):
    value = State()


class AdminChannelFeeFlow(StatesGroup):
    value = State()


class AdminChannelInstructionsFlow(StatesGroup):
    value = State()


class AdminSupplierMarkupFlow(StatesGroup):
    value = State()


class AdminSupplierMinimumProfitFlow(StatesGroup):
    value = State()


class AdminReferralConfigFlow(StatesGroup):
    value = State()
