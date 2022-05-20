odoo.define('ada-payment-module.processing', function(require) {
    'use strict';

    var publicWidget = require('web.public.widget');

    var PaymentProcessing = publicWidget.registry.PaymentProcessing;

    return PaymentProcessing.include({
        init: function() {
            this._super.apply(this, arguments);
            this._inProgressClock = false;
            this._expiredTime = false;
            this.pagePresent = false // Prevent updating the same block every time, ensure the first time
        },
        processPolledData: function(transactions) {
            this._super.apply(this, arguments);
            if (!this.pagePresent) { // Ensure the first page
                this._super.apply(this, arguments);
                this.pagePresent = true
            }
            if (!(transactions[0].adapay_status === 'new')) {
                // On "new" status, we need to prevent the updating every time, but we need to update the first time
                //this._super.apply(this, arguments);
            }
            if (!transactions[0].return_url) {
                this.$el.find("a").removeAttr("href")
            }
            if (!this._inProgressClock) {
                if (transactions[0].adapay_expiration_seconds) {
                    this._inProgressClock = true;
                    let now = new Date();
                    this._expiredTime = now.getTime() + transactions[0].adapay_expiration_seconds * 1000;

                    var interval_id = setInterval(() => {
                        let now = new Date()
                        let diff = this._expiredTime - now.getTime();
                        if (diff > 0) {
                            let diff_minutes = Math.floor(diff / 60000);
                            let diff_seconds = Math.floor((diff - diff_minutes * 60000) / 1000);
                            this.$el.find('#expiration_clock').html(String(diff_minutes).padStart(2, '0') + ":" + String(diff_seconds).padStart(2, '0'));
                        } else {
                            clearInterval(interval_id);
                        }
                    }, 200)
                }
            }

        },
    });
});