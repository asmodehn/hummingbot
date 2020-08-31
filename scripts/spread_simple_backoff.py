import sys
from decimal import Decimal
import traceback
from hummingbot.script.script_base import ScriptBase
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    OrderCancelledEvent, SellOrderCompletedEvent,
)

s_decimal_1 = Decimal("1")


class SpreadSimpleBackoff(ScriptBase):
    """
    Simply backing off spread (double) after filled order. slowly reducing overtime (substract tolerance pct).
    """

    original_bid_spread: Decimal
    original_ask_spread: Decimal

    def __init__(self):
        super().__init__()
        self.tickcounter = 0

        self.original_bid_spread = None
        self.original_ask_spread = None

        self.bought_orders = []
        self.sold_orders = []

        self.mid_price_previous = None

    def compute_trade_delta(self):
        base_trade = Decimal(0)
        quote_trade = Decimal(0)
        for b in self.bought_orders:
            base_trade += b.base_asset_amount
            quote_trade -= b.quote_asset_amount
        for s in self.sold_orders:
            base_trade -= s.base_asset_amount
            quote_trade += s.quote_asset_amount

        return base_trade, quote_trade

    def compute_trade_value(self):
        base_trade, quote_trade = self.compute_trade_delta()
        return base_trade * self.mid_price + quote_trade

    def compute_min_profit_price(self):
        """compute minimal profit price.
        Either the current price, or the minimal price for profit if this one is higher...
        """
        b, q = self.compute_trade_delta()
        min_profit_price = abs(q / b)   # if neg -> price has to be positive anyway, if pos -> already fine

        # but we should strive for current price if higher
        return max(self.mid_price, min_profit_price)

    def on_status(self) -> str:
        try:
            b, q = self.compute_trade_delta()
            status_extra = f"{self.__class__.__name__}: Active " \
                           f"\n Trade Delta: base: {b} quote: {q} Trade Value (quote): {self.compute_trade_value()}"
            if b > 0:
                min_profit_price = self.compute_min_profit_price()
                status_extra += f"\n -> min_profit_price: {min_profit_price}" \
                                f"\n -> bid_spread: {self.pmm_parameters.bid_spread} ask_spread: {self.pmm_parameters.ask_spread}"
            return status_extra

        except Exception:
            excstr = traceback.format_exception(*sys.exc_info(), limit=None, chain=True)
            self.notify(excstr)

    def on_tick(self):  # called every second...
        try:
            if self.original_ask_spread is None:  # only on startup
                self.original_ask_spread = self.pmm_parameters.ask_spread
            if self.original_bid_spread is None:  # only on startup
                self.original_bid_spread = self.pmm_parameters.bid_spread

            if self.mid_price_previous is None:  # only on startup
                self.mid_price_previous = self.mid_price

            self.tickcounter += 1
            if self.tickcounter == int(
                    self.pmm_parameters.order_refresh_time):  # TMP better to be triggered on event by strategy...
                self.tickcounter = 0  # reset
                # order refresh period ended  # TODO : hookup to on_order_cancel()
                self.on_order_refresh_period_ends()

        except Exception:
            excstr = traceback.format_exception(*sys.exc_info(), limit=None, chain=True)
            self.notify(excstr)

    def on_buy_order_completed(self, event: BuyOrderCompletedEvent):
        """
        Is called upon a buy order is completely filled.
        It is intended to be implemented by the derived class of this class.
        """
        try:
            self.bought_orders.append(event)
            b, q = self.compute_trade_delta()
            extra_bought = max(0, divmod(b, self.pmm_parameters.order_amount)[0])

            # reincreasing the spread to avoid staying too close to mid price
            notif = f"bid_spread: {self.pmm_parameters.bid_spread} ->"
            self.pmm_parameters.bid_spread *= extra_bought + 1  # increasing spread proportionally to the extra bought orders
            self.notify(f"{notif} {self.pmm_parameters.bid_spread}")

        except Exception:
            excstr = traceback.format_exception(*sys.exc_info(), limit=None, chain=True)
            self.notify(excstr)

    def on_sell_order_completed(self, event: SellOrderCompletedEvent):
        """
        Is called upon a sell order is completely filled.
        It is intended to be implemented by the derived class of this class.
        """
        try:
            self.sold_orders.append(event)
            b, q = self.compute_trade_delta()
            extra_sold = max(0, divmod(-b, self.pmm_parameters.order_amount)[0])

            # reincreasing the spread to avoid staying too close to mid price
            notif = f"ask_spread: {self.pmm_parameters.ask_spread} ->"
            self.pmm_parameters.ask_spread *= extra_sold + 1  # increasing spread proportionally to the extra sold orders
            self.notify(f"{notif} {self.pmm_parameters.ask_spread}")

        except Exception:
            excstr = traceback.format_exception(*sys.exc_info(), limit=None, chain=True)
            self.notify(excstr)

    def on_order_refresh_period_ends(self):

        # Correct spread
        bd, qd = self.compute_trade_delta()
        self.notify(f"Trade Delta: base:{bd} quote:{qd}")
        if bd == 0 or qd == 0:
            #  we still need to reduce the spread... assume minimal spread as original total spread:
            min_spread = self.original_bid_spread + self.original_ask_spread
        else:
            min_profit_price = self.compute_min_profit_price()
            # self.notify(f"min_profit_price: {min_profit_price}")

            # calculating minimal spread necessary for profit
            # if closed orders balanced it's the total spread, otherwise its only one sided!
            min_spread = abs(self.mid_price - min_profit_price) / self.mid_price

            # min_spread prevents from setting spreads where we will lose money...
            # Note: we use this symmetrically (dont sell too cheap & don't buy too high)
            # for a lack of a better heuristic here...
            # we use the amount in the orders to dynamically balance the spread.

        extra_bought = divmod(bd, self.pmm_parameters.order_amount)[0]
        if extra_bought < -1:  # careful with divmod and negative number !
            notif = "More sold than bought!"
            if self.mid_price_previous > self.mid_price:
                notif += f"{notif} Trend DOWN {self.mid_price - self.mid_price_previous} -> conserving spread..."
            elif self.pmm_parameters.bid_spread - self.pmm_parameters.order_refresh_tolerance_pct > min_spread:
                notif = f"{notif} bid_spread: {self.pmm_parameters.bid_spread} ->"
                self.pmm_parameters.bid_spread -= self.pmm_parameters.order_refresh_tolerance_pct  # reducing bid spread by significant amount
                notif = f"{notif} {self.pmm_parameters.bid_spread}"
            self.notify(notif)

        elif extra_bought > 0:
            notif = "More bought than sold!"
            if self.mid_price_previous < self.mid_price:
                notif += f"{notif} Trend UP {self.mid_price - self.mid_price_previous} -> conserving spread..."
            elif self.pmm_parameters.ask_spread - self.pmm_parameters.order_refresh_tolerance_pct > min_spread:
                notif = f"{notif} ask_spread: {self.pmm_parameters.ask_spread} ->"
                self.pmm_parameters.ask_spread -= self.pmm_parameters.order_refresh_tolerance_pct  # reducing ask spread by significant amount
                notif = f"{notif} {self.pmm_parameters.ask_spread}"
            self.notify(notif)

        else:
            notif = "Stable mid price, keeping spread..."  # message will be replaced if needed

            # reducing total spread by significant amount
            if self.mid_price_previous > self.mid_price:  # Trend DOWN -> we can reduce ask_spread
                notif = "Trend DOWN ->"
                if self.pmm_parameters.ask_spread - Decimal(0.5) * self.pmm_parameters.order_refresh_tolerance_pct > min_spread / 2:
                    notif = f"{notif} ask_spread: {self.pmm_parameters.ask_spread} ->"
                    self.pmm_parameters.ask_spread -= Decimal(0.5) * self.pmm_parameters.order_refresh_tolerance_pct  # reducing bid spread by significant amount
                    notif = f"{notif} {self.pmm_parameters.ask_spread}"
                else:
                    notif = f"{notif} already at minimum ask_spread"

            if self.mid_price_previous < self.mid_price:  # Trend UP -> we can reduce bid_spread
                notif = "Trend UP ->"
                if self.pmm_parameters.bid_spread - Decimal(0.5) * self.pmm_parameters.order_refresh_tolerance_pct > min_spread / 2:
                    notif = f"{notif} bid_spread: {self.pmm_parameters.bid_spread} ->"
                    self.pmm_parameters.bid_spread -= Decimal(0.5) * self.pmm_parameters.order_refresh_tolerance_pct
                    notif = f"{notif} {self.pmm_parameters.bid_spread}"
                else:
                    notif = f"{notif} already at minimum bid_spread"
            self.notify(notif)

    def on_order_cancelled(self, event: OrderCancelledEvent):
        try:
            # TODO : is it working yet ?
            self.notify(f"OrderCancelledEvent: {event}")

        except Exception:
            excstr = traceback.format_exception(*sys.exc_info(), limit=None, chain=True)
            self.notify(excstr)
